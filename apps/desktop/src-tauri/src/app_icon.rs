use std::{io, ptr};
use windows_sys::Win32::{
    Foundation::HWND,
    System::LibraryLoader::GetModuleHandleW,
    UI::{
        HiDpi::{GetDpiForWindow, GetSystemMetricsForDpi},
        WindowsAndMessaging::{
            LoadImageW, SendMessageW, ICON_BIG, ICON_SMALL, IMAGE_ICON, LR_SHARED, SM_CXICON,
            SM_CXSMICON, SM_CYICON, SM_CYSMICON, WM_SETICON,
        },
    },
};

// APP_ICON_RESOURCE_ID（应用图标资源编号）：与 Tauri（桌面框架）的嵌入资源保持一致。
const APP_ICON_RESOURCE_ID: usize = 32512;

/// apply（按当前显示缩放应用图标）；hwnd（原生窗口句柄）。
pub fn apply(hwnd: HWND) -> io::Result<()> {
    // DPI（显示像素密度）：直接选择对应的图标帧，避免将 32 像素位图缩成模糊的标题栏图标。
    unsafe {
        let module = GetModuleHandleW(ptr::null());
        if module.is_null() {
            return Err(io::Error::last_os_error());
        }
        let dpi = GetDpiForWindow(hwnd).max(96);
        for (kind, width_metric, height_metric) in [
            (ICON_SMALL, SM_CXSMICON, SM_CYSMICON),
            (ICON_BIG, SM_CXICON, SM_CYICON),
        ] {
            let icon = LoadImageW(
                module,
                APP_ICON_RESOURCE_ID as *const u16,
                IMAGE_ICON,
                GetSystemMetricsForDpi(width_metric, dpi),
                GetSystemMetricsForDpi(height_metric, dpi),
                LR_SHARED,
            );
            if icon.is_null() {
                return Err(io::Error::last_os_error());
            }
            // LR_SHARED（系统共享图标）由进程资源缓存管理，不重复创建或释放图标句柄。
            SendMessageW(hwnd, WM_SETICON, kind as usize, icon as isize);
        }
    }
    Ok(())
}
