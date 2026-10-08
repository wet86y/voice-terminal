// Generic resource transport（通用资源传输）：复用固定 DesktopUpdateKit（桌面下载组件）的 WinHTTP（系统网络库）。
#include <DesktopUpdateKit/UpdateKit.h>
#include <windows.h>
#include <bcrypt.h>
#include <algorithm>
#include <fstream>
#include <iomanip>
#include <sstream>
#include <stdexcept>
#include <cstring>

using Progress = int (*)(void*, std::uint64_t, std::uint64_t); // Progress（进度回调）：非零表示取消。
static std::string hash_file(const std::filesystem::path& path, bool sha512) {
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
        std::ifstream stream(path, std::ios::binary); if (!stream) throw std::runtime_error("无法读取校验文件");
        std::vector<char> buffer(1024 * 1024);
        while (stream) { stream.read(buffer.data(), buffer.size()); auto count = stream.gcount();
            if (count && BCryptHashData(hash, reinterpret_cast<PUCHAR>(buffer.data()), static_cast<ULONG>(count), 0) < 0)
                throw std::runtime_error("文件摘要计算失败"); }
        if (BCryptFinishHash(hash, digest.data(), hash_size, 0) < 0) throw std::runtime_error("文件摘要计算失败");
    } catch (...) { BCryptDestroyHash(hash); BCryptCloseAlgorithmProvider(algorithm, 0); throw; }
    BCryptDestroyHash(hash); BCryptCloseAlgorithmProvider(algorithm, 0);
    std::ostringstream value; value << std::hex << std::setfill('0');
    for (auto byte : digest) value << std::setw(2) << static_cast<int>(byte);
    return value.str();
}

extern "C" int vt_download(const char* url, const wchar_t* target, std::uint64_t size,
    const char* expected, int sha512, std::uint64_t probe_limit, Progress progress, void* context,
    char* error, std::size_t error_size) noexcept {
    try {
        using namespace desktop_update_kit;
        auto cancelled = [&](std::uint64_t current) {
            if (progress && progress(context, current, size)) throw std::runtime_error("任务已取消，断点已保留");
        };
        auto transport = make_winhttp_transport(L"VoiceTerminal-Resources/0.8");
        if (probe_limit) {
            HttpRequest request{std::string(url)};
            request.range = std::make_pair(0ull, probe_limit - 1); request.max_body_bytes = probe_limit;
            std::uint64_t read{};
            auto response = transport->get(request, [&](auto chunk) { read += chunk.size(); cancelled(read); });
            if (response.status != 200 && response.status != 206) throw std::runtime_error("下载来源连接失败");
            return static_cast<int>(read);
        }
        if (!size || !expected || !*expected) throw std::runtime_error("资源缺少可信大小或摘要");
        std::filesystem::path output(target), part(output.wstring() + L".part"), checkpoint(output.wstring() + L".part.identity");
        std::filesystem::create_directories(output.parent_path());
        if (std::filesystem::exists(output) && std::filesystem::file_size(output) == size && hash_file(output, sha512 != 0) == expected) {
            cancelled(size); return 0;
        }
        const std::string identity = std::to_string(size) + ":" + (sha512 ? "sha512:" : "sha256:") + expected;
        std::string previous; { std::ifstream in(checkpoint); std::getline(in, previous); }
        if (previous != identity || (std::filesystem::exists(part) && std::filesystem::file_size(part) > size)) {
            std::filesystem::remove(part); std::ofstream(checkpoint) << identity;
        }
        std::uint64_t offset = std::filesystem::exists(part) ? std::filesystem::file_size(part) : 0;
        std::ofstream stream(part, std::ios::binary | std::ios::app);
        if (!stream) throw std::runtime_error("下载暂存文件不可写");
        cancelled(offset);
        while (offset < size) {
            const auto length = std::min<std::uint64_t>(4ull * 1024 * 1024, size - offset);
            std::vector<std::byte> buffer; buffer.reserve(static_cast<std::size_t>(length));
            HttpRequest request{std::string(url)}; request.range = std::make_pair(offset, offset + length - 1);
            request.max_body_bytes = length;
            HttpResponse response;
            int attempt = 0;
            for (;;) {
                try {
                    response = transport->get(request, [&](auto chunk) {
                        buffer.insert(buffer.end(), chunk.begin(), chunk.end()); cancelled(offset + buffer.size());
                    }); break;
                } catch (...) { cancelled(offset); if (++attempt >= 3) throw; buffer.clear(); }
            }
            if (response.status == 200 && !response.content_range) {
                // Range（范围请求）不成立：重新完整流式传输，并在每块落盘后保存稳定断点。
                stream.close(); stream.open(part, std::ios::binary | std::ios::trunc); offset = 0;
                HttpRequest full{std::string(url)}; full.max_body_bytes = size + 1;
                auto final_response = transport->get(full, [&](auto chunk) {
                    if (offset + chunk.size() > size) throw std::runtime_error("下载超过清单声明大小");
                    stream.write(reinterpret_cast<const char*>(chunk.data()), chunk.size()); stream.flush();
                    if (!stream) throw std::runtime_error("下载文件写入失败"); offset += chunk.size(); cancelled(offset);
                });
                if (final_response.status != 200 || offset != size) throw std::runtime_error("完整下载响应不匹配");
                break;
            }
            if (response.status != 206 || !response.content_range || response.content_range->first != offset ||
                response.content_range->last != offset + length - 1 || response.content_range->total != size || buffer.size() != length)
                throw std::runtime_error("范围响应与固定资源清单不匹配");
            stream.write(reinterpret_cast<const char*>(buffer.data()), buffer.size()); stream.flush();
            if (!stream) throw std::runtime_error("下载文件写入失败"); offset += length; cancelled(offset);
        }
        stream.close();
        if (offset != size || hash_file(part, sha512 != 0) != expected) {
            std::filesystem::remove(part); throw std::runtime_error("下载文件摘要不符");
        }
        std::filesystem::remove(output); std::filesystem::rename(part, output); std::filesystem::remove(checkpoint);
        return 0;
    } catch (const std::exception& exc) {
        if (error && error_size) { std::strncpy(error, exc.what(), error_size - 1); error[error_size - 1] = 0; }
        return -1;
    } catch (...) {
        if (error && error_size) { std::strncpy(error, "原生资源传输失败", error_size - 1); error[error_size - 1] = 0; }
        return -1;
    }
}
