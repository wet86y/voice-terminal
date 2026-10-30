#pragma once
#include <DesktopUpdateKit/UpdateKit.h>

using ResourceProgress = int (*)(void*, int, std::uint64_t, std::uint64_t, const char*); // ResourceProgress（阶段、字节及中文详情回调）。
using ResourceCancel = int (*)(const void*); // ResourceCancel（独立原子取消信号）。
extern "C" int vt_download(const char*, const wchar_t*, std::uint64_t, const char*, int, int, std::uint64_t,
    ResourceProgress, void*, ResourceCancel, const void*, char*, std::size_t) noexcept;
extern "C" int vt_download_github(const char*, const wchar_t*, std::uint64_t, const char*, int, const char*,
    ResourceProgress, void*, ResourceCancel, const void*, char*, std::size_t) noexcept;
extern "C" int vt_resource_cached(const wchar_t*, std::uint64_t, const char*, int,
    ResourceProgress, void*, ResourceCancel, const void*, char*, std::size_t) noexcept;

namespace voice_resource_download {
// download_after_cache_check（同一次来源回退）：旧缓存由调用者核验，新内容仍须核验。
int download_after_cache_check(const char*, const wchar_t*, std::uint64_t, const char*, int,
    ResourceProgress, void*, ResourceCancel, const void*, char*, std::size_t) noexcept;
// transfer_with_locked_kit（锁定上游传输桥接）：进度回调始终在调用线程执行。
void transfer_with_locked_kit(desktop_update_kit::HttpTransport& transport, const std::string& url,
    const std::filesystem::path& path, std::uint64_t size,
    const std::function<void(std::uint64_t, int, bool, double)>& report);
}
