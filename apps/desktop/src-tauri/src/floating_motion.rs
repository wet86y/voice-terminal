//! 悬浮窗在桌面线程应用实际位置和尺寸，网页命中区域随窗口一起移动。
use crate::floating_geometry::{Bounds, Point};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
use tauri::WebviewWindow;

#[cfg(windows)]
mod desktop_timer {
    use std::cell::RefCell;
    use std::collections::HashMap;
    use std::panic::{catch_unwind, AssertUnwindSafe};
    use std::ptr::null_mut;
    use windows_sys::Win32::Foundation::HWND;
    use windows_sys::Win32::UI::WindowsAndMessaging::{KillTimer, SetTimer};

    type TimerTask = Box<dyn FnMut() -> bool>; // TimerTask（返回是否需要下一帧）。
    thread_local! {
        static TASKS: RefCell<HashMap<usize, TimerTask>> = RefCell::new(HashMap::new());
    }

    unsafe extern "system" fn tick(_: HWND, _: u32, timer: usize, _: u32) {
        // 调整原生窗口可能重入消息循环，先取出任务，避免重复执行同一帧。
        let Some(mut task) = TASKS.with(|tasks| tasks.borrow_mut().remove(&timer)) else {
            return;
        };
        let repeat = catch_unwind(AssertUnwindSafe(&mut task)).unwrap_or(false);
        if repeat {
            TASKS.with(|tasks| tasks.borrow_mut().insert(timer, task));
        } else {
            unsafe { KillTimer(null_mut(), timer) };
        }
    }

    pub(super) fn start(
        interval_ms: u32,
        task: impl FnMut() -> bool + 'static,
    ) -> Result<(), String> {
        let timer = unsafe { SetTimer(null_mut(), 0, interval_ms, Some(tick)) };
        if timer == 0 {
            return Err(format!(
                "无法开始悬浮窗计时：{}",
                std::io::Error::last_os_error()
            ));
        }
        TASKS.with(|tasks| tasks.borrow_mut().insert(timer, Box::new(task)));
        Ok(())
    }
}

/// start_window_timer（窗口计时）：在桌面线程启动，任务返回是否继续更新。
pub fn start_window_timer(
    window: &WebviewWindow,
    interval_ms: u32,
    task: impl FnMut() -> bool + Send + 'static,
) -> Result<(), String> {
    #[cfg(windows)]
    {
        let _ = window;
        desktop_timer::start(interval_ms, task)
    }
    #[cfg(not(windows))]
    {
        let window = window.clone();
        std::thread::spawn(move || {
            let mut task = task;
            loop {
                std::thread::sleep(Duration::from_millis(u64::from(interval_ms)));
                let (reply, done) = std::sync::mpsc::channel();
                if window
                    .run_on_main_thread(move || {
                        let repeat = task();
                        let _ = reply.send((task, repeat));
                    })
                    .is_err()
                {
                    break;
                }
                match done.recv() {
                    Ok((next, true)) => task = next,
                    _ => break,
                }
            }
        });
        Ok(())
    }
}

#[derive(Clone, Copy, PartialEq, Eq)]
pub enum Motion {
    Instant, // Instant（立即布局）：用于首帧和显示器变化。
    Animate, // Animate（过渡至目标）：贴边、脱离、悬停。
    Follow,  // Follow（跟随拖动）：沿用未完成的过渡，否则直接定位。
}

#[derive(Default)]
struct MotionState {
    generation: u64,        // generation（窗口过渡代次）。
    target: Option<Bounds>, // target（当前过渡目标）。
}

#[derive(Default)]
pub struct WindowMotion(Arc<Mutex<MotionState>>);

/// DisplayClip（展示窗口圆角区域）：只在切换状态时更新系统裁切和鼠标命中区。
#[derive(Default)]
pub struct DisplayClip(Arc<Mutex<MotionState>>);

pub fn window_bounds(window: &WebviewWindow) -> Result<Bounds, String> {
    let position = window.outer_position().map_err(|error| error.to_string())?;
    let size = window.outer_size().map_err(|error| error.to_string())?;
    Ok(Bounds {
        origin: Point {
            x: f64::from(position.x),
            y: f64::from(position.y),
        },
        width: f64::from(size.width),
        height: f64::from(size.height),
    })
}

fn reposition(window: &WebviewWindow, bounds: Bounds, above_orb: bool) -> Result<(), String> {
    #[cfg(windows)]
    {
        use windows_sys::Win32::UI::WindowsAndMessaging::{
            SetWindowPos, HWND_TOPMOST, SWP_NOACTIVATE, SWP_NOZORDER,
        };
        let handle = window.hwnd().map_err(|error| error.to_string())?;
        // 展示窗可以覆盖球体宿主的透明边缘，命中区仍随实际展示边界移动。
        let (after, flags) = if above_orb {
            (HWND_TOPMOST, SWP_NOACTIVATE)
        } else {
            (std::ptr::null_mut(), SWP_NOACTIVATE | SWP_NOZORDER)
        };
        let result = unsafe {
            SetWindowPos(
                handle.0,
                after,
                bounds.origin.x.round() as i32,
                bounds.origin.y.round() as i32,
                bounds.width.round() as i32,
                bounds.height.round() as i32,
                flags,
            )
        };
        if result == 0 {
            return Err(std::io::Error::last_os_error().to_string());
        }
    }
    #[cfg(not(windows))]
    {
        let _ = above_orb;
        window
            .set_position(tauri::PhysicalPosition::new(
                bounds.origin.x.round() as i32,
                bounds.origin.y.round() as i32,
            ))
            .map_err(|error| error.to_string())?;
        window
            .set_size(tauri::PhysicalSize::new(
                bounds.width.round() as u32,
                bounds.height.round() as u32,
            ))
            .map_err(|error| error.to_string())?;
    }
    Ok(())
}

fn animations_enabled() -> bool {
    #[cfg(windows)]
    {
        use windows_sys::Win32::UI::WindowsAndMessaging::{
            SystemParametersInfoW, SPI_GETCLIENTAREAANIMATION,
        };
        let mut enabled = 1_i32;
        unsafe {
            SystemParametersInfoW(
                SPI_GETCLIENTAREAANIMATION,
                0,
                (&mut enabled as *mut i32).cast(),
                0,
            )
        };
        enabled != 0
    }
    #[cfg(not(windows))]
    true
}

fn apply_clip(window: &WebviewWindow, bounds: Bounds) -> Result<(), String> {
    #[cfg(windows)]
    {
        use crate::floating_geometry::{DISPLAY_CLIP_MARGIN, DISPLAY_WIDTH};
        use windows_sys::Win32::Graphics::Gdi::{CreateRoundRectRgn, DeleteObject, SetWindowRgn};
        let handle = window.hwnd().map_err(|error| error.to_string())?;
        let diameter = (24. * bounds.width / DISPLAY_WIDTH).round() as i32;
        // 网页负责绘制抗锯齿圆角。系统区域略大，给描边和分数像素留出空间。
        let clip = bounds.enclosing_pixels(DISPLAY_CLIP_MARGIN);
        let region = unsafe {
            CreateRoundRectRgn(
                clip.origin.x as i32,
                clip.origin.y as i32,
                clip.right() as i32,
                clip.bottom() as i32,
                diameter,
                diameter,
            )
        };
        if region.is_null() {
            return Err("无法建立展示框的圆角区域。".into());
        }
        // 系统成功接管后负责释放区域。
        if unsafe { SetWindowRgn(handle.0, region, 1) } == 0 {
            unsafe { DeleteObject(region) };
            return Err("无法应用展示框的圆角区域。".into());
        }
    }
    #[cfg(not(windows))]
    let _ = (window, bounds);
    Ok(())
}

fn clear_clip(window: &WebviewWindow) -> Result<(), String> {
    #[cfg(windows)]
    {
        use windows_sys::Win32::Graphics::Gdi::SetWindowRgn;
        let handle = window.hwnd().map_err(|error| error.to_string())?;
        if unsafe { SetWindowRgn(handle.0, std::ptr::null_mut(), 1) } == 0 {
            return Err("无法展开图文展示的临时绘制区域。".into());
        }
    }
    #[cfg(not(windows))]
    let _ = window;
    Ok(())
}

impl DisplayClip {
    pub fn cancel(&self) {
        self.0.lock().unwrap().target = None;
    }

    pub fn apply(
        &self,
        window: &WebviewWindow,
        target: Bounds,
        defer: bool, // defer（等合成层完成过渡后再收紧裁切）。
    ) -> Result<(), String> {
        let (previous, generation) = {
            let mut state = self.0.lock().unwrap();
            if state.target == Some(target) {
                return Ok(());
            }
            state.generation += 1;
            (state.target.replace(target), state.generation)
        };
        let moving = previous
            .filter(|_| defer && animations_enabled() && window.is_visible().unwrap_or(false));
        let Some(previous) = moving else {
            if let Err(error) = apply_clip(window, target) {
                self.cancel();
                return Err(error);
            }
            return Ok(());
        };
        // 换边会改变卡片在窗口内的偏移。为移动中的圆角留出整块透明画布。
        if target.origin.x < previous.origin.x
            || target.origin.y < previous.origin.y
            || target.right() > previous.right()
            || target.bottom() > previous.bottom()
        {
            if let Err(error) = clear_clip(window) {
                self.cancel();
                return Err(error);
            }
        }
        // 允许合成层先完成收放，随后收紧系统命中区域，避免中途裁切正文。
        let state = self.0.clone();
        let render = window.clone();
        let scheduled = start_window_timer(window, 200, move || {
            let current = {
                let state = state.lock().unwrap();
                state.generation == generation && state.target == Some(target)
            };
            if current && apply_clip(&render, target).is_err() {
                state.lock().unwrap().target = None;
            }
            false
        });
        if scheduled.is_err() {
            if let Err(error) = apply_clip(window, target) {
                self.cancel();
                return Err(error);
            }
        }
        Ok(())
    }
}

impl WindowMotion {
    pub fn cancel(&self) {
        self.0.lock().unwrap().target = None;
    }

    pub fn active(&self) -> bool {
        self.0.lock().unwrap().target.is_some()
    }

    pub fn apply(
        &self,
        window: &WebviewWindow,
        target: Bounds,
        motion: Motion,
        above_orb: bool,
    ) -> Result<(), String> {
        if motion == Motion::Follow {
            if let Some(pending) = self.0.lock().unwrap().target.as_mut() {
                *pending = target;
                return Ok(());
            }
        }
        self.cancel();
        if motion != Motion::Animate
            || !animations_enabled()
            || !window.is_visible().unwrap_or(false)
        {
            return reposition(window, target, above_orb);
        }
        let origin = window_bounds(window)?;
        if origin == target {
            return reposition(window, target, above_orb);
        }
        let generation = {
            let mut state = self.0.lock().unwrap();
            state.generation += 1;
            state.target = Some(target);
            state.generation
        };
        let state = self.0.clone();
        let render = window.clone();
        let start = Instant::now();
        let duration = Duration::from_millis(160); // 与共享界面的常规过渡时长一致。
        let scheduled = start_window_timer(window, 12, move || {
            let fraction = (start.elapsed().as_secs_f64() / duration.as_secs_f64()).min(1.);
            let target = {
                let mut state = state.lock().unwrap();
                if state.generation != generation {
                    return false;
                }
                if fraction == 1. {
                    state.target.take()
                } else {
                    state.target
                }
            };
            let Some(target) = target else { return false };
            let eased = 1. - (1. - fraction).powi(3);
            if reposition(&render, origin.interpolate(target, eased), above_orb).is_err() {
                let mut state = state.lock().unwrap();
                if state.generation == generation {
                    state.target = None;
                }
                return false;
            }
            fraction < 1.
        });
        if scheduled.is_err() {
            self.cancel();
            return reposition(window, target, above_orb);
        }
        Ok(())
    }
}
