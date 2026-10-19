// Generic resource transport（通用资源传输）：复用固定 DesktopUpdateKit（桌面下载组件）的 WinHTTP（系统网络库）。
#include <DesktopUpdateKit/UpdateKit.h>
#include "locked_kit_transfer.h"
#include <windows.h>
#include <bcrypt.h>
#include <algorithm>
#include <fstream>
#include <iomanip>
#include <sstream>
#include <stdexcept>
#include <cstring>
#include <chrono>
#include <functional>
#include <thread>

using Progress = ResourceProgress; // Progress（进度回调）：阶段 1 传输、2 摘要、3 启用、4 缓存、5 四路、6 回退，非零取消。
using CancelCheck = ResourceCancel; // CancelCheck（线程安全取消检查）：只读独立原子信号，不调用界面回调。
struct DownloadFailure : std::runtime_error { // DownloadFailure（分类失败）：-2 本地文件、-3 取消、-4 不可恢复。
    int kind;
    DownloadFailure(int code, const std::string& message) : std::runtime_error(message), kind(code) {}
};
static std::string hash_file(const std::filesystem::path& path, bool sha512, const std::function<void(std::uint64_t)>& report) {
    BCRYPT_ALG_HANDLE algorithm{}; BCRYPT_HASH_HANDLE hash{};
    DWORD object_size{}, hash_size{}, bytes{};
    if (BCryptOpenAlgorithmProvider(&algorithm, sha512 ? BCRYPT_SHA512_ALGORITHM : BCRYPT_SHA256_ALGORITHM, nullptr, 0) < 0)
        throw std::runtime_error("摘要算法初始化失败");
    BCryptGetProperty(algorithm, BCRYPT_OBJECT_LENGTH, reinterpret_cast<PUCHAR>(&object_size), sizeof(object_size), &bytes, 0);
    BCryptGetProperty(algorithm, BCRYPT_HASH_LENGTH, reinterpret_cast<PUCHAR>(&hash_size), sizeof(hash_size), &bytes, 0);
    std::vector<UCHAR> object(object_size), digest(hash_size);
    if (BCryptCreateHash(algorithm, &hash, object.data(), object_size, nullptr, 0, 0) < 0) {
        BCryptCloseAlgorithmProvider(algorithm, 0); throw std::runtime_error("摘要初始化失败");
    }
    try {
        std::ifstream stream(path, std::ios::binary); if (!stream) throw DownloadFailure(-2, "无法读取校验文件");
        std::vector<char> buffer(1024 * 1024);std::uint64_t completed{};report(0);
        while (stream) { stream.read(buffer.data(), buffer.size()); auto count = stream.gcount();
            if (count && BCryptHashData(hash, reinterpret_cast<PUCHAR>(buffer.data()), static_cast<ULONG>(count), 0) < 0)
                throw std::runtime_error("文件摘要计算失败");completed += count;report(completed); }
        if (!stream.eof()) throw DownloadFailure(-2, "文件摘要读取失败");
        if (BCryptFinishHash(hash, digest.data(), hash_size, 0) < 0) throw std::runtime_error("文件摘要计算失败");
    } catch (...) { BCryptDestroyHash(hash); BCryptCloseAlgorithmProvider(algorithm, 0); throw; }
    BCryptDestroyHash(hash); BCryptCloseAlgorithmProvider(algorithm, 0);
    std::ostringstream value; value << std::hex << std::setfill('0');
    for (auto byte : digest) value << std::setw(2) << static_cast<int>(byte);
    return value.str();
}

// promote_download（启用已校验下载）：短暂共享冲突可重试，失败保留断点与原缓存。
static void promote_download(const std::filesystem::path& part, const std::filesystem::path& output,
    const std::function<void()>& cancelled) {
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(5); // deadline（文件占用重试上限）。
    for (;;) {
        cancelled();
        // MOVEFILE_REPLACE_EXISTING（直接替换目标）避免先删除缓存再重命名的空档。
        if (MoveFileExW(part.c_str(), output.c_str(), MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH)) return;
        const DWORD code = GetLastError(); // code（Windows 系统错误码，不包含可能乱码的文件路径）。
        const bool retryable = code == ERROR_SHARING_VIOLATION || code == ERROR_LOCK_VIOLATION || code == ERROR_ACCESS_DENIED; // retryable（文件占用或短暂访问拒绝）。
        if (!retryable)
            throw DownloadFailure(-4, "下载文件启用失败（系统错误码：" + std::to_string(code) + "），下载断点已保留。");
        if (std::chrono::steady_clock::now() >= deadline)
            throw DownloadFailure(code == ERROR_ACCESS_DENIED ? -4 : -2, std::string(code == ERROR_ACCESS_DENIED ? "系统仍拒绝启用下载文件" : "下载文件仍被其他进程占用") +
                "（系统错误码：" + std::to_string(code) + "），断点已保留，请稍后重试下载。");
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }
}

extern "C" int vt_download(const char* url, const wchar_t* target, std::uint64_t size,
    const char* expected, int sha512, int accelerate, std::uint64_t probe_limit, Progress progress, void* context, CancelCheck cancel_check, const void* cancel_context,
    char* error, std::size_t error_size) noexcept {
    try {
        using namespace desktop_update_kit;
        auto cancelled = [&](std::uint64_t current, int phase = 1, const char* detail = nullptr) {
            if (cancel_check && cancel_check(cancel_context)) throw DownloadFailure(-3, "任务已取消，断点已保留");
            if (progress && progress(context, phase, current, size, detail)) throw DownloadFailure(-3, "任务已取消，断点已保留");
        };
        auto verifying = [&](std::uint64_t current) { cancelled(current, 2); };
        cancelled(0);
        auto transport = make_winhttp_transport(L"VoiceTerminal-Resources/0.8");
        std::stop_source network_stop;std::jthread network_watch;
        // fetch（受控请求）：无响应或读取停滞时也能主动中断句柄，等待线程在返回前收集。
        auto fetch = [&](const HttpRequest& request, const HttpTransport::ChunkSink& sink) {
            auto bounded = request; // bounded（单次网络等待）：允许无整文件时限的大模型续传。
            bounded.connect_timeout = std::chrono::seconds(12);
            bounded.idle_timeout = std::chrono::seconds(20);
            if (!network_watch.joinable() && cancel_check) network_watch = std::jthread([&](std::stop_token token) {
                while (!token.stop_requested()) {
                    if (cancel_check(cancel_context)) {network_stop.request_stop();transport->abort_active();}
                    std::this_thread::sleep_for(std::chrono::milliseconds(50));
                }
            });
            return transport->get(bounded, sink, network_stop.get_token());
        };
        if (probe_limit) {
            HttpRequest request{std::string(url)};
            request.range = std::make_pair(0ull, probe_limit - 1); request.max_body_bytes = probe_limit;
            std::uint64_t read{};
            auto response = fetch(request, [&](auto chunk) { read += chunk.size(); cancelled(read); });
            if (response.status != 200 && response.status != 206) throw std::runtime_error("下载来源连接失败");
            return static_cast<int>(read);
        }
        if (!size || !expected || !*expected) throw std::runtime_error("资源缺少可信大小或摘要");
        std::filesystem::path output(target), part(output.wstring() + L".part"), checkpoint(output.wstring() + L".part.identity");
        std::filesystem::create_directories(output.parent_path());
        if (std::filesystem::exists(output) && std::filesystem::file_size(output) == size && hash_file(output, sha512 != 0, [&](auto done) { cancelled(done, 4); }) == expected) {
            cancelled(size, 8); cancelled(size, 3); return 0;
        }
        const std::string identity = std::to_string(size) + ":" + (sha512 ? "sha512:" : "sha256:") + expected;
        std::string previous; { std::ifstream in(checkpoint); std::getline(in, previous); }
        if (previous != identity || (std::filesystem::exists(part) && std::filesystem::file_size(part) > size)) {
            std::filesystem::remove(part); std::ofstream(checkpoint) << identity;
        }
        std::uint64_t offset = std::filesystem::exists(part) ? std::filesystem::file_size(part) : 0;
        // accelerate（复用上游四路）：已有连续断点优先续传；新下载的大型 GitHub（代码托管平台）文件使用锁定算法。
        if (accelerate && offset == 0) {
            try {
                voice_resource_download::transfer_with_locked_kit(*transport, url, part, size,
                    [&](auto done, int connections, bool fallback, double speed) {
                        const int phase = fallback ? 6 : connections > 1 ? 5 : 1;
                        std::ostringstream detail;
                        detail << (fallback ? "下载固定资源（四路加速失败，已回退单路下载）" : connections > 1 ? "下载固定资源（四路加速）" : "下载固定资源")
                            << " · " << std::fixed << std::setprecision(2) << speed / (1024 * 1024) << " MiB/秒（兆二进制字节每秒）";
                        cancelled(done, phase, detail.str().c_str());
                    });
            } catch (const std::exception& failure) {
                cancelled(0);
                const std::string message = failure.what();
                if (message == "Unable to create update file" || message == "Unable to create range file" ||
                    message == "Unable to create merged update file" || message == "Unable to write update file")
                    throw DownloadFailure(-4, "下载暂存文件不可写，请检查磁盘空间与目录权限；断点已保留。");
                throw;
            }
            offset = size;
        }
        std::ofstream stream(part, std::ios::binary | std::ios::app);
        if (!stream) throw DownloadFailure(-4, "下载暂存文件不可写，请检查磁盘空间与目录权限");
        cancelled(offset);
        constexpr std::uint64_t range_bytes = 8ull * 1024 * 1024; // range_bytes（单个范围的有界缓冲）：减少大模型的请求次数。
        std::vector<std::byte> buffer;buffer.reserve(static_cast<std::size_t>(std::min(range_bytes, size)));
        while (offset < size) {
            const auto length = std::min<std::uint64_t>(range_bytes, size - offset);buffer.clear();
            HttpRequest request{std::string(url)}; request.range = std::make_pair(offset, offset + length - 1);
            request.max_body_bytes = length;
            HttpResponse response;
            int attempt = 0;
            for (;;) {
                try {
                    response = fetch(request, [&](auto chunk) {
                        buffer.insert(buffer.end(), chunk.begin(), chunk.end()); cancelled(offset + buffer.size());
                    }); break;
                } catch (...) { cancelled(offset); if (++attempt >= 3) throw; buffer.clear(); }
            }
            if (response.status == 200 && !response.content_range) {
                // Range（范围请求）不成立：重新完整流式传输，并在每块落盘后保存稳定断点。
                stream.close(); stream.open(part, std::ios::binary | std::ios::trunc); offset = 0;
                if (!stream) throw DownloadFailure(-4, "完整下载暂存文件不可写，请检查磁盘空间与目录权限");
                std::uint64_t flushed{}; // flushed（最近落盘断点）：每 4 MiB 或最终完成时刷新。
                HttpRequest full{std::string(url)}; full.max_body_bytes = size + 1;
                auto final_response = fetch(full, [&](auto chunk) {
                    if (offset + chunk.size() > size) throw std::runtime_error("下载超过清单声明大小");
                    stream.write(reinterpret_cast<const char*>(chunk.data()), chunk.size());
                    if (offset + chunk.size() - flushed >= 4ull * 1024 * 1024 || offset + chunk.size() == size) {stream.flush();flushed = offset + chunk.size();}
                    if (!stream) throw DownloadFailure(-4, "下载文件写入失败，请检查磁盘空间与目录权限"); offset += chunk.size(); cancelled(offset);
                });
                if (final_response.status != 200 || offset != size) throw std::runtime_error("完整下载响应不匹配");
                break;
            }
            // ModelScope（魔搭）小文件可返回 HTTP 200（成功）及精确 Content-Range（字节范围）。
            // 两种成功状态都必须匹配固定范围、总大小与实际字节数，完整文件仍需摘要校验。
            if ((response.status != 206 && response.status != 200) || !response.content_range || response.content_range->first != offset ||
                response.content_range->last != offset + length - 1 || response.content_range->total != size || buffer.size() != length)
                throw std::runtime_error("范围响应与固定资源清单不匹配");
            stream.write(reinterpret_cast<const char*>(buffer.data()), buffer.size()); stream.flush();
            if (!stream) throw DownloadFailure(-4, "下载文件写入失败，请检查磁盘空间与目录权限"); offset += length; cancelled(offset);
        }
        stream.close();
        if (!stream) throw DownloadFailure(-2, "下载暂存文件关闭失败，断点已保留。");
        if (offset != size || hash_file(part, sha512 != 0, verifying) != expected) {
            std::filesystem::remove(part); throw std::runtime_error("下载文件摘要不符");
        }
        promote_download(part, output, [&] { cancelled(size, 3); });
        // cleanup_error（断点清理错误）：已启用完整文件后，辅助记录占用不应使下载变成失败。
        std::error_code cleanup_error; std::filesystem::remove(checkpoint, cleanup_error);
        return 0;
    } catch (const DownloadFailure& exc) {
        if (error && error_size) { std::strncpy(error, exc.what(), error_size - 1); error[error_size - 1] = 0; }
        return exc.kind;
    } catch (const std::filesystem::filesystem_error& exc) {
        const auto code = exc.code().value();
        const auto message = "本地文件操作失败（系统错误码：" + std::to_string(code) + "），断点已保留。";
        if (error && error_size) { std::strncpy(error, message.c_str(), error_size - 1); error[error_size - 1] = 0; }
        return code == ERROR_SHARING_VIOLATION || code == ERROR_LOCK_VIOLATION ? -2 : -4;
    } catch (const std::exception& exc) {
        if (error && error_size) { std::strncpy(error, exc.what(), error_size - 1); error[error_size - 1] = 0; }
        return -1;
    } catch (...) {
        if (error && error_size) { std::strncpy(error, "原生资源传输失败", error_size - 1); error[error_size - 1] = 0; }
        return -1;
    }
}

// vt_fetch_manifest（读取应用更新清单）：固定 HTTPS（加密传输）地址、响应大小和超时。
extern "C" int vt_resource_cached(const wchar_t* target, std::uint64_t size, const char* expected, int sha512,
    ResourceProgress progress, void* context, ResourceCancel cancel_check, const void* cancel_context, char* error, std::size_t error_size) noexcept {
    try {
        auto report = [&](std::uint64_t done, int phase) {
            if ((cancel_check && cancel_check(cancel_context)) || (progress && progress(context, phase, done, size, nullptr)))
                throw DownloadFailure(-3, "任务已取消，断点已保留");
        };
        if (cancel_check && cancel_check(cancel_context)) throw DownloadFailure(-3, "任务已取消，断点已保留");
        const std::filesystem::path output(target);
        if (!std::filesystem::exists(output) || std::filesystem::file_size(output) != size) return 0;
        report(0, 4);
        if (hash_file(output, sha512 != 0, [&](auto done) { report(done, 4); }) != expected) return 0;
        report(size, 8); report(size, 3); return 1;
    } catch (const DownloadFailure& failure) {
        if (error && error_size) { std::strncpy(error, failure.what(), error_size - 1); error[error_size - 1] = 0; }
        return failure.kind;
    } catch (...) {
        if (error && error_size) { std::strncpy(error, "现有下载缓存无法读取，旧缓存已保留。", error_size - 1); error[error_size - 1] = 0; }
        return -2;
    }
}

extern "C" int vt_fetch_manifest(const char* url, unsigned char* output, std::size_t output_size,
    char* error, std::size_t error_size) noexcept {
    try {
        if (!url || !*url || !output || output_size < 2) throw std::runtime_error("更新清单请求参数无效");
        desktop_update_kit::HttpRequest request{std::string(url)};
        request.connect_timeout = std::chrono::seconds(12);
        request.idle_timeout = std::chrono::seconds(20);
        request.max_body_bytes = output_size - 1;
        auto transport = desktop_update_kit::make_winhttp_transport(L"VoiceTerminal-Updates/0.9");
        std::size_t written{};
        const auto response = transport->get(request, [&](std::span<const std::byte> chunk) {
            if (chunk.size() > output_size - 1 - written) throw std::runtime_error("更新清单超出允许大小");
            std::memcpy(output + written, chunk.data(), chunk.size());
            written += chunk.size();
        });
        if (response.status != 200) throw std::runtime_error("GitHub 发行清单请求失败（HTTP 状态 " + std::to_string(response.status) + "）");
        output[written] = 0;
        return static_cast<int>(written);
    } catch (const std::exception& exc) {
        if (error && error_size) { std::strncpy(error, exc.what(), error_size - 1); error[error_size - 1] = 0; }
        return -1;
    } catch (...) {
        if (error && error_size) { std::strncpy(error, "读取 GitHub 发行清单失败", error_size - 1); error[error_size - 1] = 0; }
        return -1;
    }
}
