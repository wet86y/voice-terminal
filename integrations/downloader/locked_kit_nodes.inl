// GitHub nodes（代码托管平台节点）：同编译单元复用锁定上游的节点校验、排序、缓存和传输。
#include "locked_kit_transfer.h"

namespace voice_resource_download {
namespace {
namespace kit = desktop_update_kit;
namespace wire = desktop_update_kit::json;

struct NodePolicy {
    kit::ClientOptions options; // options（上游节点缓存标识）。
    kit::Release release; // release（显式节点列表，不更改可信资源身份）。
    bool acceleration{true}; // acceleration（允许加速站，默认开启）。
    bool auto_select{true}; // auto_select（当前网络测速择优）。
    int timeout_ms{2000}; // timeout_ms（单节点探测与测速总时限）。
    std::uint64_t sample_bytes{1024 * 1024}; // sample_bytes（有界测速样本大小）。
#ifdef VOICE_DOWNLOAD_QUALIFICATION
    std::vector<std::pair<std::string, std::string>> test_sources; // test_sources（仅隔离测试的本机来源映射）。
#endif
};
NodePolicy read_node_policy(const char* text) {
    wire::Value value; std::string error;
    if (!text || !wire::parse(text, value, error)) throw std::runtime_error("GitHub（代码托管平台）下载配置无效");
    const auto* object = wire::object(&value);
    if (!object) throw std::runtime_error("下载节点配置必须为对象");
    NodePolicy policy;
    const auto* app = wire::string(wire::member(*object, "application_id"));
    if (!app || app->empty() || app->size() > 100 || !std::all_of(app->begin(), app->end(), [](unsigned char c) { return std::isalnum(c) || c == '-' || c == '_'; }))
        throw std::runtime_error("下载节点缓存标识无效");
    policy.options.application_id = kit::wide(*app);
    if (const auto enabled = wire::boolean(wire::member(*object, "acceleration_enabled"))) policy.acceleration = *enabled;
    if (const auto enabled = wire::boolean(wire::member(*object, "auto_select"))) policy.auto_select = *enabled;
    if (const auto limit = wire::number(wire::member(*object, "probe_timeout_ms"))) policy.timeout_ms = static_cast<int>(std::clamp(*limit, 250.0, 5000.0));
    if (const auto limit = wire::number(wire::member(*object, "sample_bytes"))) policy.sample_bytes = static_cast<std::uint64_t>(std::clamp(*limit, 65536.0, 2.0 * 1024 * 1024));
    const auto* nodes = wire::array(wire::member(*object, "nodes"));
    if (nodes && nodes->size() > 8) throw std::runtime_error("下载加速节点不得超过八个");
    if (nodes) for (const auto& row : *nodes) {
        const auto* node = wire::object(&row); if (!node) throw std::runtime_error("下载节点记录无效");
        const auto* id = wire::string(wire::member(*node, "id")); const auto* url = wire::string(wire::member(*node, "template"));
        const auto priority = wire::number(wire::member(*node, "priority")); const auto enabled = wire::boolean(wire::member(*node, "enabled"));
        if (!id || !url || !priority || !enabled) throw std::runtime_error("下载节点缺少编号、模板、优先级或启用标记");
        auto normalized = kit::normalize_node({*id, *url, static_cast<int>(std::clamp(*priority, -10000.0, 10000.0)), *enabled});
        if (!normalized) throw std::runtime_error("下载节点模板不符合上游约定");
        policy.release.nodes.push_back(std::move(*normalized));
    }
#ifdef VOICE_DOWNLOAD_QUALIFICATION
    if (const auto* sources = wire::object(wire::member(*object, "test_sources"))) for (const auto& [origin, target] : *sources) {
        const auto* address = wire::string(&target);
        if (!address || !address->starts_with("http://127.0.0.1:")) throw std::runtime_error("隔离来源必须为本机回环服务");
        policy.test_sources.emplace_back(origin, *address);
    }
#endif
    return policy;
}

class SourceTransport final : public kit::HttpTransport {
public:
    SourceTransport(const NodePolicy& policy) : inner_(kit::make_winhttp_transport(L"VoiceTerminal-GitHubNodes/0.9")), policy_(policy) {}
    kit::HttpResponse get(const kit::HttpRequest& request, const ChunkSink& sink, std::stop_token token = {}) override {
        auto source = request;
#ifdef VOICE_DOWNLOAD_QUALIFICATION
        for (const auto& [origin, target] : policy_.test_sources) if (source.url.starts_with(origin)) { source.url = target + source.url.substr(origin.size()); break; }
#endif
        return inner_->get(source, sink, token);
    }
    void abort_active() noexcept override { inner_->abort_active(); }
    std::string actual_url(const std::string& url) const {
#ifdef VOICE_DOWNLOAD_QUALIFICATION
        for (const auto& [origin, target] : policy_.test_sources) if (url.starts_with(origin)) return target + url.substr(origin.size());
#endif
        return url;
    }
private:
    std::shared_ptr<kit::HttpTransport> inner_; // inner_（独立网络传输，取消不会干扰其他文件）。
    const NodePolicy& policy_; // policy_（当前节点及隔离配置）。
};

// NodeCacheGuard（节点缓存互斥）：上游采用固定临时名，跨进程协调后再调用其原子保存。
class NodeCacheGuard {
public:
    explicit NodeCacheGuard(const kit::ClientOptions& options) {
        const auto name = L"Local\\VoiceTerminal-NodeCache-" + kit::safe_application_id(options.application_id);
        handle_ = CreateMutexW(nullptr, FALSE, name.c_str());
        if (handle_) { const auto result = WaitForSingleObject(handle_, 1000); acquired_ = result == WAIT_OBJECT_0 || result == WAIT_ABANDONED; }
    }
    ~NodeCacheGuard() { if (acquired_) ReleaseMutex(handle_); if (handle_) CloseHandle(handle_); }
    explicit operator bool() const { return acquired_; }
private:
    HANDLE handle_{}; // handle_（跨进程互斥句柄）。
    bool acquired_{}; // acquired_（本次是否成功获得缓存写权限）。
};

double sample_speed(SourceTransport& transport, const std::string& url, std::uint64_t size,
    const NodePolicy& policy, ResourceCancel cancel, const void* context) {
    std::stop_source stop; const auto started = std::chrono::steady_clock::now();
    const auto deadline = started + std::chrono::milliseconds(policy.timeout_ms);
    std::jthread watchdog([&](std::stop_token token) {
        while (!token.stop_requested()) {
            if ((cancel && cancel(context)) || std::chrono::steady_clock::now() >= deadline) { stop.request_stop(); transport.abort_active(); break; }
            std::this_thread::sleep_for(std::chrono::milliseconds(20));
        }
    });
    kit::DownloadOptions options; options.probe_timeout = std::chrono::milliseconds(policy.timeout_ms); options.idle_timeout = options.probe_timeout;
    kit::DownloadControl control; double baseline{};
    try {
        kit::probe_node(transport, url, size, options, control, stop.get_token());
        const auto probe_time = std::chrono::duration<double>(std::chrono::steady_clock::now() - started).count();
        baseline = static_cast<double>(std::min<std::uint64_t>(size, 65536)) / std::max(probe_time, 0.001);
        if (size <= 65536) return baseline;
        const auto count = std::min(size, policy.sample_bytes); std::uint64_t received{};
        kit::HttpRequest request{url}; request.range = std::pair<std::uint64_t, std::uint64_t>{0, count - 1}; request.max_body_bytes = count;
        request.connect_timeout = options.probe_timeout; request.idle_timeout = options.idle_timeout;
        const auto sample_started = std::chrono::steady_clock::now();
        const auto response = transport.get(request, [&](std::span<const std::byte> bytes) { received += bytes.size(); }, stop.get_token());
        if (received != count || (response.status != 200 && response.status != 206)) return baseline;
        if (response.status == 206 && (!response.content_range || response.content_range->first != 0 || response.content_range->last != count - 1 || response.content_range->total != size)) return baseline;
        return static_cast<double>(count) / std::max(std::chrono::duration<double>(std::chrono::steady_clock::now() - sample_started).count(), 0.001);
    } catch (...) { return baseline; }
}

struct ForwardProgress {
    ResourceProgress callback; // callback（宿主进度回调）。
    void* context; // context（宿主回调上下文）。
    std::string node; // node（当前来源中文说明）。
};
int forward_progress(void* context, int phase, std::uint64_t done, std::uint64_t total, const char* detail) {
    auto& forward = *static_cast<ForwardProgress*>(context);
    const char* base = phase == 2 ? "校验下载文件" : phase == 3 ? "提交已校验下载缓存" : phase == 4 ? "校验现有下载缓存" : "下载固定资源";
    const auto message = std::string(detail && *detail ? detail : base) + " · 来源：" + forward.node;
    return forward.callback ? forward.callback(forward.context, phase, done, total, message.c_str()) : 0;
}
}
}

extern "C" int vt_download_github(const char* origin, const wchar_t* target, std::uint64_t size, const char* expected, int sha512,
    const char* configuration, ResourceProgress progress, void* context, ResourceCancel cancel, const void* cancel_context, char* error, std::size_t error_size) noexcept {
    using namespace voice_resource_download; using namespace desktop_update_kit;
    try {
        const int cached = vt_resource_cached(target, size, expected, sha512, progress, context, cancel, cancel_context, error, error_size);
        if (cached != 0) return cached > 0 ? 0 : cached;
        const auto policy = read_node_policy(configuration);
        DownloadControl control; control.use_acceleration(policy.acceleration); control.consume_node_switch_request();
        auto nodes = resolve_nodes(policy.options, policy.release, control.acceleration_enabled());
        auto status = [&](const std::string& text) {
            if ((cancel && cancel(cancel_context)) || (progress && progress(context, 7, 0, 0, text.c_str()))) throw std::runtime_error("任务已取消，断点已保留");
        };
        std::vector<std::pair<DownloadNode, double>> ranked;
        if (policy.auto_select && nodes.size() > 1) {
            for (const auto& node : nodes) {
                const auto label = node.id == "github-direct" ? "GitHub（代码托管平台）原站" : node.id;
                status("来源测速：" + label);
                SourceTransport transport(policy);
                const auto speed = sample_speed(transport, expand_node(node, origin), size, policy, cancel, cancel_context);
                status("来源测速：" + label + " · " + std::to_string(static_cast<int>(speed / 1024)) + " KiB/秒（千二进制字节每秒）");
                ranked.emplace_back(node, speed);
            }
            std::stable_sort(ranked.begin(), ranked.end(), [](const auto& left, const auto& right) { return left.second > right.second; });
            nodes.clear(); for (const auto& [node, speed] : ranked) nodes.push_back(node);
        }
        std::string failures;
        for (const auto& node : nodes) {
            const auto label = node.id == "github-direct" ? "GitHub（代码托管平台）原站" : node.id;
            status("下载来源：" + label);
            SourceTransport source(policy); const auto url = source.actual_url(expand_node(node, origin));
            ForwardProgress forward{progress, context, label};
            const int result = vt_download(url.c_str(), target, size, expected, sha512, 1, 0, forward_progress, &forward, cancel, cancel_context, error, error_size);
            if (result == 0) {
                NodeCacheGuard guard(policy.options);
                if (guard) save_cache(policy.options, policy.release.nodes, node.id);
                return 0;
            }
            if (result != -1) return result;
            const std::string reason = error && *error ? error : "来源传输失败";
            failures += (failures.empty() ? "" : "；") + label + "：" + reason;
            status("来源 " + label + " 失败，尝试下一来源");
        }
        if (error && error_size) { const auto message = "所有下载节点均失败：" + failures; std::strncpy(error, message.c_str(), error_size - 1); error[error_size - 1] = 0; }
        return -1;
    } catch (const std::exception& failure) {
        const bool cancelled = cancel && cancel(cancel_context);
        const auto message = cancelled ? "任务已取消，断点已保留" : failure.what();
        if (error && error_size) { std::strncpy(error, message, error_size - 1); error[error_size - 1] = 0; }
        return cancelled ? -3 : -4;
    } catch (...) {
        if (error && error_size) { std::strncpy(error, "下载节点管理失败，旧缓存已保留", error_size - 1); error[error_size - 1] = 0; }
        return -4;
    }
}
