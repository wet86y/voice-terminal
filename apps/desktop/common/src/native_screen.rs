//! 客户端截图：在同一帧上生成全局 JPEG 和物理像素局部 PNG。
use std::{ffi::c_void, fs, io, os::windows::ffi::OsStrExt, path::Path, ptr::null_mut};
use windows_sys::{
    core::GUID,
    Win32::{
        Foundation::{CloseHandle, RECT},
        Graphics::{
            Gdi::{
                BitBlt, CreateCompatibleDC, CreateDIBSection, DeleteDC, DeleteObject, GdiFlush,
                GetDC, ReleaseDC, SelectObject, BITMAPINFO, BITMAPINFOHEADER, BI_RGB, CAPTUREBLT,
                DIB_RGB_COLORS, HBITMAP, HDC, HGDIOBJ, SRCCOPY,
            },
            GdiPlus::{
                EncoderParameter, EncoderParameterValueTypeLong, EncoderParameters, EncoderQuality,
                GdipCloneBitmapAreaI, GdipCreateBitmapFromScan0, GdipDeleteGraphics,
                GdipDisposeImage, GdipDrawImageRectI, GdipGetImageGraphicsContext,
                GdipSaveImageToFile, GdipSetInterpolationMode, GdiplusShutdown, GdiplusStartup,
                GdiplusStartupInput, GpBitmap, InterpolationModeHighQualityBicubic, Status,
            },
        },
        System::{
            RemoteDesktop::ProcessIdToSessionId,
            StationsAndDesktops::{CloseDesktop, OpenInputDesktop, DESKTOP_READOBJECTS},
            Threading::{
                OpenProcess, QueryFullProcessImageNameW, PROCESS_QUERY_LIMITED_INFORMATION,
            },
        },
        UI::WindowsAndMessaging::{
            GetForegroundWindow, GetSystemMetrics, GetWindowRect, GetWindowThreadProcessId,
            SM_CXSCREEN, SM_CYSCREEN,
        },
    },
};

const RGB32: i32 = 0x0002_2009; // RGB32（GDI+ 32 位 RGB，不使用屏幕缓冲区的透明通道）。
const JPEG: GUID = GUID::from_u128(0x557cf401_1a04_11d3_9a73_0000f81ef32e); // 系统 JPEG 编码器。
const PNG: GUID = GUID::from_u128(0x557cf406_1a04_11d3_9a73_0000f81ef32e); // 系统 PNG 编码器。

/// interactive_session（交互会话能力）：远端客户端必须运行在用户会话。
pub fn interactive_session() -> bool {
    let mut session = 0;
    // SAFETY: session 是可写的 Win32 会话编号。
    unsafe { ProcessIdToSessionId(std::process::id(), &mut session) != 0 && session != 0 }
}

/// capture_to_file（截屏并写双图）：按 JPEG、PNG 顺序合并到一份二进制传输文件。
/// 两个路径均由宿主在同一个新建受管目录中生成。
pub fn capture_to_file(
    bundle: &Path,
    local_file: &Path,
    max_side: i32,
    requested_crop: (i32, i32),
) -> Result<serde_json::Value, String> {
    if !interactive_session() || max_side <= 0 || requested_crop.0 <= 0 || requested_crop.1 <= 0 {
        return Err("截屏参数无效或当前没有交互用户会话。".into());
    }
    // SAFETY: 持有输入桌面读取权限后才复制屏幕，所有 Win32 句柄均在本线程回收。
    let (width, height) = unsafe {
        let desktop = OpenInputDesktop(0, 0, DESKTOP_READOBJECTS);
        if desktop.is_null() {
            return Err("当前交互桌面不可读取，可能处于锁屏状态。".into());
        }
        CloseDesktop(desktop);
        (GetSystemMetrics(SM_CXSCREEN), GetSystemMetrics(SM_CYSCREEN))
    };
    if width <= 0 || height <= 0 {
        return Err("主屏幕尺寸无效。".into());
    }
    let (rect, executable) = foreground();
    let midpoint = |first, last| (i64::from(first) + i64::from(last)).div_euclid(2) as i32;
    let (x, y) = (
        midpoint(rect.left, rect.right),
        midpoint(rect.top, rect.bottom),
    );
    let (x, y, center_source) = if rect.right > rect.left
        && rect.bottom > rect.top
        && (0..width).contains(&x)
        && (0..height).contains(&y)
    {
        (x, y, "window_center")
    } else {
        (width / 2, height / 2, "screen_center")
    };
    let crop_width = requested_crop.0.min(width);
    let crop_height = requested_crop.1.min(height);
    let left = (x - crop_width / 2).clamp(0, width - crop_width);
    let top = (y - crop_height / 2).clamp(0, height - crop_height);
    let _graphics = GdiPlus::start()?;
    let pixels = ScreenPixels::capture(width, height)?;
    // GDI+ 原图借用 pixels（屏幕像素缓冲区），在 pixels 释放之前销毁。
    let frame = Bitmap::from_pixels(width, height, pixels.address.cast())?;
    let local = frame.crop(left, top, crop_width, crop_height)?;
    let longest = width.max(height);
    let scaled = if longest > max_side {
        let scale = f64::from(max_side) / f64::from(longest);
        Some(frame.resize(
            ((f64::from(width) * scale).round() as i32).max(1),
            ((f64::from(height) * scale).round() as i32).max(1),
        )?)
    } else {
        None
    };
    let mut quality = 85u32; // quality（全局 JPEG 编码质量）：与主机路径一致。
    let parameters = EncoderParameters {
        Count: 1,
        Parameter: [EncoderParameter {
            Guid: EncoderQuality,
            NumberOfValues: 1,
            Type: EncoderParameterValueTypeLong as u32,
            Value: (&mut quality as *mut u32).cast(),
        }],
    };
    scaled
        .as_ref()
        .unwrap_or(&frame)
        .save(bundle, &JPEG, Some(&parameters))?;
    local.save(local_file, &PNG, None)?;
    let global_bytes = fs::metadata(bundle)
        .map_err(|error| error.to_string())?
        .len();
    let mut global = fs::OpenOptions::new()
        .append(true)
        .open(bundle)
        .map_err(|error| error.to_string())?;
    let mut local = fs::File::open(local_file).map_err(|error| error.to_string())?;
    io::copy(&mut local, &mut global).map_err(|error| error.to_string())?;
    Ok(serde_json::json!({
        "monitor_width":width, "monitor_height":height, "center_x":x, "center_y":y,
        "center_source":center_source, "executable":executable, "crop_left":left, "crop_top":top,
        "crop_width":crop_width, "crop_height":crop_height, "global_bytes":global_bytes,
        "fields":{"monitor_width":"屏幕宽度","monitor_height":"屏幕高度","center_x":"裁剪中心横坐标",
            "center_y":"裁剪中心纵坐标","center_source":"裁剪中心来源","executable":"前台进程路径",
            "crop_left":"局部图左边界","crop_top":"局部图上边界","crop_width":"局部图宽度",
            "crop_height":"局部图高度","global_bytes":"全局 JPEG 字节数，也是局部 PNG 起始偏移"}
    }))
}

fn foreground() -> (RECT, String) {
    let mut rect = RECT::default();
    let mut path = [0u16; 1024];
    let mut size = path.len() as u32;
    // SAFETY: 缓冲区大小与 Win32 参数一致；查询失败时退回主屏幕中心。
    unsafe {
        let hwnd = GetForegroundWindow();
        if hwnd.is_null() || GetWindowRect(hwnd, &mut rect) == 0 {
            return (RECT::default(), String::new());
        }
        let mut pid = 0;
        GetWindowThreadProcessId(hwnd, &mut pid);
        let process = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid);
        if process.is_null() {
            return (rect, String::new());
        }
        let ok = QueryFullProcessImageNameW(process, 0, path.as_mut_ptr(), &mut size);
        CloseHandle(process);
        (
            rect,
            if ok != 0 {
                String::from_utf16_lossy(&path[..size as usize])
            } else {
                String::new()
            },
        )
    }
}

#[derive(Default)]
struct ScreenPixels {
    display: HDC,
    memory: HDC,
    bitmap: HBITMAP,
    previous: HGDIOBJ,
    address: *mut c_void,
}

impl ScreenPixels {
    fn capture(width: i32, height: i32) -> Result<Self, String> {
        let mut pixels = Self::default();
        // SAFETY: 逐步登记句柄，失败时由 Drop（作用域清理）按逆序释放。
        unsafe {
            pixels.display = GetDC(null_mut());
            if pixels.display.is_null() {
                return Err("无法取得主屏幕设备上下文。".into());
            }
            pixels.memory = CreateCompatibleDC(pixels.display);
            if pixels.memory.is_null() {
                return Err("无法创建截图内存设备上下文。".into());
            }
            let mut info = BITMAPINFO::default();
            info.bmiHeader = BITMAPINFOHEADER {
                biSize: std::mem::size_of::<BITMAPINFOHEADER>() as u32,
                biWidth: width,
                biHeight: -height,
                biPlanes: 1,
                biBitCount: 32,
                biCompression: BI_RGB,
                ..Default::default()
            };
            pixels.bitmap = CreateDIBSection(
                pixels.display,
                &info,
                DIB_RGB_COLORS,
                &mut pixels.address,
                null_mut(),
                0,
            );
            if pixels.bitmap.is_null() || pixels.address.is_null() {
                return Err("无法分配截图像素缓冲区。".into());
            }
            pixels.previous = SelectObject(pixels.memory, pixels.bitmap);
            if pixels.previous.is_null()
                || pixels.previous as isize == -1
                || BitBlt(
                    pixels.memory,
                    0,
                    0,
                    width,
                    height,
                    pixels.display,
                    0,
                    0,
                    SRCCOPY | CAPTUREBLT,
                ) == 0
                || GdiFlush() == 0
            {
                return Err("主屏幕像素复制失败。".into());
            }
        }
        Ok(pixels)
    }
}

impl Drop for ScreenPixels {
    fn drop(&mut self) {
        // SAFETY: 先恢复原位图，再释放本次持有的缓冲区和设备上下文。
        unsafe {
            if !self.previous.is_null() && self.previous as isize != -1 {
                SelectObject(self.memory, self.previous);
            }
            if !self.bitmap.is_null() {
                DeleteObject(self.bitmap);
            }
            if !self.memory.is_null() {
                DeleteDC(self.memory);
            }
            if !self.display.is_null() {
                ReleaseDC(null_mut(), self.display);
            }
        }
    }
}

struct GdiPlus(usize);

impl GdiPlus {
    fn start() -> Result<Self, String> {
        let mut token = 0;
        let input = GdiplusStartupInput {
            GdiplusVersion: 1,
            SuppressExternalCodecs: 1,
            ..Default::default()
        };
        // SAFETY: 参数与令牌均在启动调用期间有效。
        check(
            unsafe { GdiplusStartup(&mut token, &input, null_mut()) },
            "初始化截图编码器",
        )?;
        Ok(Self(token))
    }
}

impl Drop for GdiPlus {
    fn drop(&mut self) {
        // SAFETY: 令牌属于本作用域，图像先于令牌释放。
        unsafe {
            GdiplusShutdown(self.0);
        }
    }
}

struct Bitmap(*mut GpBitmap);

impl Bitmap {
    fn from_pixels(width: i32, height: i32, data: *const u8) -> Result<Self, String> {
        let mut bitmap = null_mut();
        let stride = width.checked_mul(4).ok_or("截图位图跨度溢出。")?;
        // SAFETY: data 是同线程的 32 位屏幕缓冲区，或为空以创建自有位图。
        check(
            unsafe { GdipCreateBitmapFromScan0(width, height, stride, RGB32, data, &mut bitmap) },
            "创建截图位图",
        )?;
        Ok(Self(bitmap))
    }

    fn crop(&self, left: i32, top: i32, width: i32, height: i32) -> Result<Self, String> {
        let mut bitmap = null_mut();
        // SAFETY: 原位图仍存活，矩形已约束在屏幕范围内。
        check(
            unsafe { GdipCloneBitmapAreaI(left, top, width, height, RGB32, self.0, &mut bitmap) },
            "裁剪局部图",
        )?;
        Ok(Self(bitmap))
    }

    fn resize(&self, width: i32, height: i32) -> Result<Self, String> {
        let target = Self::from_pixels(width, height, std::ptr::null())?;
        let mut graphics = null_mut();
        // SAFETY: 上下文在目标位图存活期间使用，并在本次缩放后立即释放。
        unsafe {
            check(
                GdipGetImageGraphicsContext(target.0.cast(), &mut graphics),
                "创建全局缩放画布",
            )?;
            let result = check(
                GdipSetInterpolationMode(graphics, InterpolationModeHighQualityBicubic),
                "设置全局缩放模式",
            )
            .and_then(|()| {
                check(
                    GdipDrawImageRectI(graphics, self.0.cast(), 0, 0, width, height),
                    "缩放全局图",
                )
            });
            GdipDeleteGraphics(graphics);
            result?;
        }
        Ok(target)
    }

    fn save(
        &self,
        path: &Path,
        codec: &GUID,
        parameters: Option<&EncoderParameters>,
    ) -> Result<(), String> {
        let path: Vec<u16> = path.as_os_str().encode_wide().chain(Some(0)).collect();
        // SAFETY: 文件名以零结尾，编码参数的所有引用有效至写图结束。
        check(
            unsafe {
                GdipSaveImageToFile(
                    self.0.cast(),
                    path.as_ptr(),
                    codec,
                    parameters.map_or(std::ptr::null(), |item| item),
                )
            },
            "保存截图",
        )
    }
}

impl Drop for Bitmap {
    fn drop(&mut self) {
        // SAFETY: 位图归此实例独占。
        unsafe {
            GdipDisposeImage(self.0.cast());
        }
    }
}

fn check(status: Status, action: &str) -> Result<(), String> {
    if status == 0 {
        Ok(())
    } else {
        Err(format!("{action}失败，GDI+（图像接口）状态：{status}。"))
    }
}
