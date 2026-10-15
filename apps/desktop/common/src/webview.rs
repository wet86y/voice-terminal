//! Evergreen（自动更新网页引擎）：只检测微软正式运行时，不把 Edge 浏览器当作运行时。
pub const ONLINE: &str = "https://go.microsoft.com/fwlink/p/?LinkId=2124703";
pub const OFFLINE: &str = "https://developer.microsoft.com/microsoft-edge/webview2/#download-section";

/// DISABLE_CONTEXT_MENU（禁用网页右键菜单）：在页面脚本运行前覆盖所有程序窗口。
pub const DISABLE_CONTEXT_MENU: &str = "document.addEventListener('contextmenu', event => event.preventDefault(), { capture: true });";

pub fn version() -> Option<String> {
    #[cfg(windows)] unsafe {
        use windows_sys::Win32::System::Registry::*;
        let wide = |s: &str| s.encode_utf16().chain(Some(0)).collect::<Vec<_>>();
        for (hive, flags) in [(HKEY_CURRENT_USER, KEY_READ | KEY_WOW64_64KEY),
            (HKEY_LOCAL_MACHINE, KEY_READ | KEY_WOW64_32KEY), (HKEY_LOCAL_MACHINE, KEY_READ | KEY_WOW64_64KEY)] {
            let mut key = std::ptr::null_mut();
            let path = wide(r"Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}");
            if RegOpenKeyExW(hive, path.as_ptr(), 0, flags, &mut key) != 0 { continue; }
            let mut buffer = [0u16; 256]; let mut size = 512; let mut kind = 0;
            let result = RegQueryValueExW(key, wide("pv").as_ptr(), std::ptr::null(), &mut kind,
                buffer.as_mut_ptr() as _, &mut size);
            RegCloseKey(key);
            if result == 0 && kind == REG_SZ {
                let text = String::from_utf16_lossy(&buffer[..buffer.iter().position(|v| *v == 0).unwrap_or(256)]);
                if text.split('.').all(|p| p.parse::<u32>().is_ok()) && text.split('.').any(|p| p.parse::<u32>().unwrap_or(0)>0) {
                    return Some(text);
                }
            }
        }
    }
    None
}

pub fn guide(reason: &str) {
    #[cfg(windows)] unsafe {
        use windows_sys::Win32::UI::WindowsAndMessaging::*;
        let wide = |s: &str| s.encode_utf16().chain(Some(0)).collect::<Vec<_>>();
        let body = format!("{reason}\n\n需要 Microsoft WebView2 Evergreen Runtime（微软自动更新网页引擎）。\n\n是：打开微软官方在线引导器\n否：打开微软官方离线安装页面\n取消：退出\n\n安装完成后请重新打开语音终端。");
        let choice = MessageBoxW(std::ptr::null_mut(), wide(&body).as_ptr(), wide("网页引擎尚不可用").as_ptr(), MB_YESNOCANCEL | MB_ICONINFORMATION);
        let url = match choice { IDYES => Some(ONLINE), IDNO => Some(OFFLINE), _ => None };
        if let Some(url) = url { let _ = crate::windows::open_url(url); }
    }
}
