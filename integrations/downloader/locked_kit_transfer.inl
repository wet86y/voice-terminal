// 此文件追加到锁定的 UpdateKit.cpp（上游实现）编译单元，直接复用内部传输算法。
// 上游公开 UpdateClient（更新客户端）要求独立摘要附件，不适用于通用资源清单。
// 不复制范围调度、分片写入、合并或单路传输的实现；升级锁定版本时必须重新验收此桥接。
#include "locked_kit_transfer.h"

namespace voice_resource_download {
static_assert(desktop_update_kit::DownloadOptions{}.max_connections == 4);
static_assert(desktop_update_kit::DownloadOptions{}.parallel_threshold == 16ull * 1024 * 1024);

// BoundedTransport（有界传输适配）：沿用上游请求，仅限制响应体，防止超过可信清单大小。
class BoundedTransport final : public desktop_update_kit::HttpTransport {
public:
    BoundedTransport(desktop_update_kit::HttpTransport& inner, std::uint64_t size) : inner_(inner), size_(size) {}
    desktop_update_kit::HttpResponse get(const desktop_update_kit::HttpRequest& request,
        const ChunkSink& sink, std::stop_token token = {}) override {
        auto bounded = request;
        const auto limit = request.range ? request.range->second - request.range->first + 1 : size_;
        if (!bounded.max_body_bytes || *bounded.max_body_bytes > limit + 1) bounded.max_body_bytes = limit + 1;
        std::uint64_t received{};
        return inner_.get(bounded, [&](std::span<const std::byte> bytes) {
            if (bytes.size() > limit - received) throw std::runtime_error("下载超过清单声明大小");
            received += bytes.size(); sink(bytes);
        }, token);
    }
    void abort_active() noexcept override { inner_.abort_active(); }
private:
    desktop_update_kit::HttpTransport& inner_; // inner_（锁定上游的系统网络传输）。
    std::uint64_t size_; // size_（可信文件总字节数）。
};

void transfer_with_locked_kit(desktop_update_kit::HttpTransport& transport, const std::string& url,
    const std::filesystem::path& path, std::uint64_t size,
    const std::function<void(std::uint64_t, int, bool, double)>& report) {
    using namespace desktop_update_kit;
    // FragmentCleanup（分片清理）：进程中断遗留分片也不能被当作连续断点复用。
    struct FragmentCleanup {
        std::filesystem::path path; // path（连续暂存文件路径）。
        void clean() const noexcept {
            for (int index = 0; index < 4; ++index) {
                std::error_code ignored;
                std::filesystem::remove(path.wstring() + L".part" + std::to_wstring(index), ignored);
            }
        }
        ~FragmentCleanup() { clean(); }
    } fragments{path};
    fragments.clean();
    BoundedTransport bounded(transport, size);
    DownloadOptions options;
    options.retries_per_node = 0; // retries_per_node（节点内部重试次数）：恢复轮次由宿主统一管理。
    DownloadControl control;
    std::stop_source stop;
    std::mutex mutex;
    std::condition_variable changed;
    DownloadProgress latest; // latest（最近一次上游进度）：工作线程不调用宿主回调。
    bool finished{};
    std::exception_ptr failure;
    std::jthread worker([&] {
        try {
            const auto probe = probe_node(bounded, url, size, options, control, stop.get_token());
            ProgressMeter meter(size, "locked-upstream", 1, false, [&](const DownloadProgress& value) {
                { std::scoped_lock lock(mutex); latest = value; }
                changed.notify_one();
            });
            if (probe.ranges && size >= options.parallel_threshold && options.max_connections > 1) {
                try {
                    download_parallel(bounded, url, path, size, options, control, meter, stop.get_token());
                } catch (...) {
                    if (control.cancelled() || stop.stop_requested()) throw;
                    std::error_code ignored; std::filesystem::remove(path, ignored);
                    download_single(bounded, url, path, size, options, control, meter, true, stop.get_token());
                }
            } else {
                download_single(bounded, url, path, size, options, control, meter, false, stop.get_token());
            }
        } catch (...) { failure = std::current_exception(); }
        { std::scoped_lock lock(mutex); finished = true; }
        changed.notify_one();
    });
    try {
        for (;;) {
            DownloadProgress snapshot;
            bool done;
            {
                std::unique_lock lock(mutex);
                changed.wait_for(lock, std::chrono::milliseconds(50));
                snapshot = latest; done = finished;
            }
            report(snapshot.received, snapshot.connections, snapshot.parallel_fallback, snapshot.bytes_per_second);
            if (done) break;
        }
    } catch (...) {
        stop.request_stop(); control.cancel(); bounded.abort_active();
        worker.join();
        throw;
    }
    worker.join();
    if (failure) std::rethrow_exception(failure);
}
}
