use serde::{Deserialize, Serialize};

#[derive(Clone, Copy, Debug, Default, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Dock {
    #[default]
    Free, // Free（自由位置）。
    Left,
    Right,
    Top,
    Bottom,
}

impl Dock {
    fn lateral(self) -> bool {
        matches!(self, Self::Left | Self::Right)
    }
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Point {
    pub x: f64, // x（屏幕横坐标，物理像素）。
    pub y: f64, // y（屏幕纵坐标，物理像素）。
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Bounds {
    pub origin: Point, // origin（左上角）。
    pub width: f64,    // width（物理像素宽度）。
    pub height: f64,   // height（物理像素高度）。
}

impl Bounds {
    pub fn right(self) -> f64 {
        self.origin.x + self.width
    }
    pub fn bottom(self) -> f64 {
        self.origin.y + self.height
    }
    pub fn center(self) -> Point {
        Point {
            x: self.origin.x + self.width / 2.,
            y: self.origin.y + self.height / 2.,
        }
    }
    fn union(self, other: Self) -> Self {
        let x = self.origin.x.min(other.origin.x);
        let y = self.origin.y.min(other.origin.y);
        Self {
            origin: Point { x, y },
            width: self.right().max(other.right()) - x,
            height: self.bottom().max(other.bottom()) - y,
        }
    }
    pub fn enclosing_pixels(self, margin: f64) -> Self {
        let x = self.origin.x.floor() - margin;
        let y = self.origin.y.floor() - margin;
        Self {
            origin: Point { x, y },
            width: self.right().ceil() + margin - x,
            height: self.bottom().ceil() + margin - y,
        }
    }
    pub fn interpolate(self, target: Self, progress: f64) -> Self {
        let mix = |start, end| start + (end - start) * progress;
        Self {
            origin: Point {
                x: mix(self.origin.x, target.origin.x),
                y: mix(self.origin.y, target.origin.y),
            },
            width: mix(self.width, target.width),
            height: mix(self.height, target.height),
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct WorkArea {
    pub bounds: Bounds, // bounds（扣除任务栏等系统区域后的可用范围）。
    pub scale: f64,     // scale（物理像素与逻辑像素的比值）。
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Placement {
    pub anchor: Point,  // anchor（球体视觉中心，可落在屏幕边缘）。
    pub dock: Dock,     // dock（吸附边）。
    pub area: WorkArea, // area（球体所在显示器的工作区）。
}

#[derive(Clone, Copy)]
pub struct DisplaySurface {
    pub canvas: Bounds, // canvas（固定窗口范围，包含圆角描边的透明余量）。
    compact: Bounds,    // compact（紧凑卡片的实际屏幕范围）。
    expanded: Bounds,   // expanded（展开卡片的实际屏幕范围）。
    scale: f64,         // scale（当前显示器缩放系数）。
}

#[derive(PartialEq, Serialize)]
pub struct DisplayFrame {
    x: f64,     // x（卡片相对固定窗口的横坐标，逻辑像素）。
    y: f64,     // y（卡片相对固定窗口的纵坐标，逻辑像素）。
    scale: f64, // scale（相对紧凑卡片的放大比例）。
}

#[derive(PartialEq, Serialize)]
pub struct OrbFrame {
    orb_x: f64,         // orb_x（窗口内菱形中心横坐标，逻辑像素）。
    orb_y: f64,         // orb_y（窗口内菱形中心纵坐标，逻辑像素）。
    caption_x: f64,     // caption_x（字幕朝菱形一侧的横坐标，逻辑像素）。
    caption_y: f64,     // caption_y（字幕顶部坐标，逻辑像素）。
    caption_align: i32, // caption_align（字幕按近边对齐的百分比）。
    caption_width: f64, // caption_width（字幕最大宽度，逻辑像素）。
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum Side {
    Left,
    Right,
}

impl Side {
    fn position(self, edge: f64, width: f64) -> f64 {
        match self {
            Self::Left => edge - width,
            Self::Right => edge,
        }
    }

    fn space(self, edge: f64, work: Bounds) -> f64 {
        match self {
            Self::Left => edge - work.origin.x,
            Self::Right => work.right() - edge,
        }
    }

    fn near_edge(self, bounds: Bounds) -> f64 {
        match self {
            Self::Left => bounds.right(),
            Self::Right => bounds.origin.x,
        }
    }
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum DisplayPosition {
    Beside(Side),         // Beside（位于菱形一侧）。
    CenteredStack,        // CenteredStack（和识别框上下排列，展示框朝菱形居中）。
    UpperSideStack(Side), // UpperSideStack（侧边上顶角，展示框在识别框上方）。
}

#[derive(Clone, Copy)]
struct DockedPanels {
    caption: Bounds,                   // caption（字幕占用区域，物理像素）。
    caption_side: Side,                // caption_side（字幕所在侧）。
    display_position: DisplayPosition, // display_position（展示框在贴边布局中的位置）。
}

impl DisplaySurface {
    pub fn region(self, focused: bool) -> Bounds {
        let bounds = if focused { self.expanded } else { self.compact };
        Bounds {
            origin: Point {
                x: bounds.origin.x - self.canvas.origin.x,
                y: bounds.origin.y - self.canvas.origin.y,
            },
            ..bounds
        }
    }

    pub fn frame(self, focused: bool) -> DisplayFrame {
        let region = self.region(focused);
        DisplayFrame {
            x: region.origin.x / self.scale,
            y: region.origin.y / self.scale,
            scale: (region.width / DISPLAY_WIDTH / self.scale)
                .min(region.height / DISPLAY_HEIGHT / self.scale),
        }
    }
}

// 与共享悬浮窗样式采用同一套逻辑尺寸，保留球体光晕及字幕的实际空间。
pub const ORB_WIDTH: f64 = 140.;
pub const ORB_HEIGHT: f64 = 158.;
const ORB_CENTER_Y: f64 = 62.;
// ORB_OUTLINE_RADIUS（满幅外圈半径）：含音量幅度缩放。
const ORB_OUTLINE_RADIUS: f64 = 54.;
// ORB_MAX_OUTLINE_EDGE（满幅外圈边长）：悬浮窗主体 70.5 逻辑像素，外圈 1.26 倍，满幅再放大 5%。
const ORB_MAX_OUTLINE_EDGE: f64 = 70.5 * 1.26 * 1.05;
const CAPTION_NEAR: f64 = 40.;
const CAPTION_FAR: f64 = 90.; // 58 像素主动间距加 32 像素字幕框高度。
const CAPTION_WIDTH: f64 = 126.; // CAPTION_WIDTH（识别框通用宽度上限，逻辑像素）。
const CAPTION_HEIGHT: f64 = 32.;
const PANEL_GAP: f64 = 6.;
// ORB_CLEARANCE（外圈加间距的占用半径）。
const ORB_CLEARANCE: f64 = ORB_OUTLINE_RADIUS + PANEL_GAP;
// SIDE_PANEL_OFFSET（近角的横纵偏移）：从斜边中点垂直向外留隙，再投影到横轴和纵轴。
const SIDE_PANEL_OFFSET: f64 =
    (ORB_MAX_OUTLINE_EDGE / 2. + PANEL_GAP) * std::f64::consts::FRAC_1_SQRT_2;
pub const DISPLAY_WIDTH: f64 = 256.;
pub const DISPLAY_HEIGHT: f64 = 160.;
const DISPLAY_EXPANSION: f64 = 1.25; // DISPLAY_EXPANSION（展示框悬停放大比例）。
pub const DISPLAY_CLIP_MARGIN: f64 = 2.; // DISPLAY_CLIP_MARGIN（为描边抗锯齿保留的物理像素）。

pub fn snap(point: Point, area: WorkArea) -> Dock {
    let work = area.bounds;
    // 与可见球体边缘相距 16 个逻辑像素时吸附。紧贴两个边缘时水平边优先。
    let distance = 16. * area.scale;
    let half_orb = 40. * area.scale;
    [
        (Dock::Left, point.x - work.origin.x),
        (Dock::Right, work.right() - point.x),
        (Dock::Top, point.y - work.origin.y),
        (Dock::Bottom, work.bottom() - point.y),
    ]
    .into_iter()
    .filter(|(_, gap)| *gap <= distance + half_orb)
    .min_by(|a, b| a.1.total_cmp(&b.1))
    .map_or(Dock::Free, |(side, _)| side)
}

pub fn pulled_inward(dock: Dock, dx: f64, dy: f64, scale: f64) -> bool {
    let distance = match dock {
        Dock::Left => dx,
        Dock::Right => -dx,
        Dock::Top => dy,
        Dock::Bottom => -dy,
        Dock::Free => 0.,
    };
    distance >= 24. * scale
}

fn fit(value: f64, origin: f64, extent: f64, size: f64) -> f64 {
    value.clamp(origin, origin + (extent - size).max(0.))
}

impl Placement {
    pub fn new(anchor: Point, dock: Dock, area: WorkArea) -> Self {
        let work = area.bounds;
        let scale = area.scale;
        let half_width = ORB_WIDTH * scale / 2.;
        let center_y = ORB_CENTER_Y * scale;
        let height = if dock.lateral() {
            center_y + (SIDE_PANEL_OFFSET + CAPTION_HEIGHT + PANEL_GAP) * scale
        } else {
            ORB_HEIGHT * scale
        };
        let anchor = Point {
            x: match dock {
                Dock::Left => work.origin.x,
                Dock::Right => work.right(),
                _ => {
                    fit(
                        anchor.x - half_width,
                        work.origin.x,
                        work.width,
                        2. * half_width,
                    ) + half_width
                }
            },
            y: match dock {
                Dock::Top => work.origin.y,
                Dock::Bottom => work.bottom(),
                _ => fit(anchor.y - center_y, work.origin.y, work.height, height) + center_y,
            },
        };
        Self { anchor, dock, area }
    }

    pub fn orb_bounds(self) -> Bounds {
        if let Some(panels) = self.docked_panels() {
            return self.docked_orb_bounds(panels);
        }
        let scale = self.area.scale;
        Bounds {
            origin: Point {
                x: self.anchor.x - ORB_WIDTH * scale / 2.,
                y: self.anchor.y - ORB_CENTER_Y * scale,
            },
            width: ORB_WIDTH * scale,
            height: ORB_HEIGHT * scale,
        }
    }

    fn docked_orb_bounds(self, panels: DockedPanels) -> Bounds {
        let work = self.area.bounds;
        let mut layout = self.diamond().union(panels.caption);
        if matches!(panels.display_position, DisplayPosition::UpperSideStack(_)) {
            // 提前涵盖两种高度，悬停时由网页过渡识别框位置。
            for expanded in [false, true] {
                layout = layout.union(self.projected_caption(panels, Some(expanded)));
            }
        }
        let margin = PANEL_GAP * self.area.scale;
        let x = (layout.origin.x - margin).floor().max(work.origin.x);
        let y = (layout.origin.y - margin).floor().max(work.origin.y);
        let right = (layout.right() + margin).ceil().min(work.right());
        let bottom = (layout.bottom() + margin).ceil().min(work.bottom());
        Bounds {
            origin: Point { x, y },
            width: right - x,
            height: bottom - y,
        }
    }

    // display_focus（图文展示的悬停状态）：空值表示当前未展示图文。
    pub fn orb_frame(self, display_focus: Option<bool>) -> Option<OrbFrame> {
        let panels = self.docked_panels()?;
        let window = self.docked_orb_bounds(panels);
        let scale = self.area.scale;
        let caption = self.projected_caption(panels, display_focus);
        let caption_edge = panels.caption_side.near_edge(caption);
        let caption_align = match panels.caption_side {
            Side::Left => -100,
            Side::Right => 0,
        };
        Some(OrbFrame {
            orb_x: (self.anchor.x - window.origin.x) / scale,
            orb_y: (self.anchor.y - window.origin.y) / scale,
            caption_x: (caption_edge - window.origin.x) / scale,
            caption_y: (caption.origin.y - window.origin.y) / scale,
            caption_align,
            caption_width: panels.caption.width / scale,
        })
    }

    pub fn same_arrangement(self, other: Self) -> bool {
        let arrangement = |place: Self| {
            place
                .docked_panels()
                .map(|panels| (panels.caption_side, panels.display_position))
        };
        self.dock == other.dock && arrangement(self) == arrangement(other)
    }

    fn diamond(self) -> Bounds {
        let work = self.area.bounds;
        let radius = ORB_OUTLINE_RADIUS * self.area.scale;
        let x = (self.anchor.x - radius).max(work.origin.x);
        let y = (self.anchor.y - radius).max(work.origin.y);
        Bounds {
            origin: Point { x, y },
            width: (self.anchor.x + radius).min(work.right()) - x,
            height: (self.anchor.y + radius).min(work.bottom()) - y,
        }
    }

    fn panel_edge(self, side: Side) -> f64 {
        let offset = if self.dock.lateral() {
            SIDE_PANEL_OFFSET
        } else {
            ORB_CLEARANCE
        };
        let distance = offset * self.area.scale;
        match side {
            Side::Left => self.anchor.x - distance,
            Side::Right => self.anchor.x + distance,
        }
    }

    fn docked_panels(self) -> Option<DockedPanels> {
        use DisplayPosition::{Beside, CenteredStack, UpperSideStack};
        let work = self.area.bounds;
        let scale = self.area.scale;
        let gap = PANEL_GAP * scale;
        let width = (CAPTION_WIDTH * scale).min(work.width);
        let height = (CAPTION_HEIGHT * scale).min(work.height);
        let (caption_side, display_position) = match self.dock {
            Dock::Free => return None,
            Dock::Left => (Side::Right, Beside(Side::Right)),
            Dock::Right => (Side::Left, Beside(Side::Left)),
            Dock::Top | Dock::Bottom => {
                let left = Side::Left.space(self.panel_edge(Side::Left), work);
                let right = Side::Right.space(self.panel_edge(Side::Right), work);
                if left >= DISPLAY_WIDTH * scale && right >= width {
                    (Side::Right, Beside(Side::Left))
                } else if right > left {
                    (Side::Right, CenteredStack)
                } else {
                    (Side::Left, CenteredStack)
                }
            }
        };
        let x = caption_side.position(self.panel_edge(caption_side), width);
        let y = match self.dock {
            Dock::Top => self.anchor.y + 10. * scale,
            Dock::Bottom => self.anchor.y - 10. * scale - height,
            _ => self.anchor.y + SIDE_PANEL_OFFSET * scale,
        };
        let caption = Bounds {
            origin: Point {
                x: fit(x, work.origin.x, work.width, width),
                y: fit(y, work.origin.y, work.height, height),
            },
            width,
            height,
        };
        let expanded_height = (DISPLAY_HEIGHT * DISPLAY_EXPANSION * scale).min(work.height);
        let display_position =
            if self.dock.lateral() && caption.origin.y - work.origin.y < expanded_height + gap {
                UpperSideStack(caption_side)
            } else {
                display_position
            };
        Some(DockedPanels {
            caption,
            caption_side,
            display_position,
        })
    }

    fn projected_caption(self, panels: DockedPanels, display_focus: Option<bool>) -> Bounds {
        let (DisplayPosition::UpperSideStack(side), Some(expanded)) =
            (panels.display_position, display_focus)
        else {
            return panels.caption;
        };
        let work = self.area.bounds;
        let caption = panels.caption;
        let display = self.docked_display_bounds(panels, expanded);
        let x = side.position(side.near_edge(display), caption.width);
        let y = display.bottom() + PANEL_GAP * self.area.scale;
        Bounds {
            origin: Point {
                x: fit(x, work.origin.x, work.width, caption.width),
                y: fit(y, work.origin.y, work.height, caption.height),
            },
            ..caption
        }
    }

    fn docked_display_bounds(self, panels: DockedPanels, expanded: bool) -> Bounds {
        use DisplayPosition::{Beside, CenteredStack, UpperSideStack};
        let work = self.area.bounds;
        let scale = self.area.scale;
        let gap = PANEL_GAP * scale;
        let near_bottom = self.anchor.y - SIDE_PANEL_OFFSET * scale;
        let caption = panels.caption;
        let position = panels.display_position;
        let (side, edge) = match position {
            Beside(side) => {
                let mut edge = self.panel_edge(side);
                if self.dock.lateral() {
                    // 中段靠近屏幕顶部时沿原来的斜边摆位让出波动间距。
                    let tall = (DISPLAY_HEIGHT * DISPLAY_EXPANSION * scale).min(work.height);
                    let shift = (work.origin.y + tall - near_bottom)
                        .clamp(0., (ORB_CLEARANCE - SIDE_PANEL_OFFSET) * scale);
                    edge += match side {
                        Side::Left => -shift,
                        Side::Right => shift,
                    };
                }
                (Some(side), edge)
            }
            UpperSideStack(side) => {
                // 堆叠的展示框跨过菱形高度，横向按完整波动外圈留隙。
                let extension = ORB_CLEARANCE * scale;
                let edge = match side {
                    Side::Left => self.anchor.x - extension,
                    Side::Right => self.anchor.x + extension,
                };
                (Some(side), edge)
            }
            CenteredStack => (None, self.anchor.x),
        };
        let centered_inset = if position == CenteredStack {
            gap.min(work.width / 4.)
        } else {
            0.
        };
        let usable_width = work.width - 2. * centered_inset;
        let width_limit = side.map_or(usable_width, |side| side.space(edge, work));
        let diamond_clearance = ORB_CLEARANCE * scale;
        let (top, bottom) = match (self.dock, position) {
            (Dock::Top, CenteredStack) => (
                (caption.bottom() + gap).max(self.anchor.y + diamond_clearance),
                work.bottom(),
            ),
            (Dock::Bottom, CenteredStack) => (
                work.origin.y,
                (caption.origin.y - gap).min(self.anchor.y - diamond_clearance),
            ),
            (Dock::Top, _) => (work.origin.y + gap, work.bottom()),
            (Dock::Bottom, _) => (work.origin.y, work.bottom() - gap),
            (_, UpperSideStack(_)) => (work.origin.y, work.bottom() - caption.height - gap),
            _ => (work.origin.y, work.bottom()),
        };
        let base_width = DISPLAY_WIDTH * scale;
        let base_height = DISPLAY_HEIGHT * scale;
        let factor = (if expanded { DISPLAY_EXPANSION } else { 1. })
            .min(width_limit.max(1.) / base_width)
            .min((bottom - top).max(1.) / base_height);
        let width = base_width * factor;
        let height = base_height * factor;
        let x = side.map_or(edge - width / 2., |side| side.position(edge, width));
        let y = match (self.dock, position) {
            (Dock::Top, _) => top,
            (Dock::Bottom, _) => bottom - height,
            (_, UpperSideStack(_)) => fit(near_bottom - height, top, bottom - top, height),
            _ => near_bottom - height,
        };
        Bounds {
            origin: Point {
                x: fit(x, work.origin.x + centered_inset, usable_width, width),
                y: fit(y, work.origin.y, work.height, height),
            },
            width,
            height,
        }
    }

    pub fn display_surface(self) -> DisplaySurface {
        let expanded = self.display_bounds(true);
        DisplaySurface {
            // 画布比系统裁切再多留一个像素，避免窗口自身贴着描边取整。
            canvas: expanded.enclosing_pixels(DISPLAY_CLIP_MARGIN + 1.),
            compact: self.display_bounds(false),
            expanded,
            scale: self.area.scale,
        }
    }

    pub fn display_bounds(self, expanded: bool) -> Bounds {
        if let Some(panels) = self.docked_panels() {
            return self.docked_display_bounds(panels, expanded);
        }
        let work = self.area.bounds;
        let scale = self.area.scale;
        let base_width = DISPLAY_WIDTH * scale;
        let base_height = DISPLAY_HEIGHT * scale;
        let gap = PANEL_GAP * scale;
        let diamond = self.diamond();
        let caption = Bounds {
            origin: Point {
                x: self.anchor.x - CAPTION_WIDTH * scale / 2.,
                y: self.anchor.y + CAPTION_NEAR * scale,
            },
            width: CAPTION_WIDTH * scale,
            height: (CAPTION_FAR - CAPTION_NEAR) * scale,
        };
        // 侧向展示通常贴近菱形上半部。靠屏幕顶端以后，卡片整体下移。
        let side_y = |height| {
            fit(
                self.anchor.y + 18. * scale - height,
                work.origin.y,
                work.height,
                height,
            )
        };
        let expanded_height = (base_height * DISPLAY_EXPANSION).min(work.height);
        let side_top = side_y(expanded_height);
        let vertical_gap =
            (caption.origin.y - side_top - expanded_height).max(side_top - caption.bottom());
        // 只有接近字幕时才渐增横向让位。两种尺寸用同一锚点，放大时鼠标仍在框中。
        let clearance = (1. - vertical_gap / (12. * scale)).clamp(0., 1.);
        let left_edge =
            diamond.origin.x - (diamond.origin.x - caption.origin.x).max(0.) * clearance;
        let right_edge = diamond.right() + (caption.right() - diamond.right()).max(0.) * clearance;
        let top_edge = diamond.origin.y.min(caption.origin.y);
        let bottom_edge = diamond.bottom().max(caption.bottom());
        let capacity = |side| match side {
            Dock::Left => (left_edge - gap - work.origin.x, work.height),
            Dock::Right => (work.right() - gap - right_edge, work.height),
            Dock::Top => (work.width, top_edge - gap - work.origin.y),
            Dock::Bottom => (work.width, work.bottom() - gap - bottom_edge),
            Dock::Free => (work.width, work.height),
        };
        let third = (self.anchor.x - work.origin.x) / work.width;
        let preferred = if third > 2. / 3. {
            [Dock::Left, Dock::Right, Dock::Top, Dock::Bottom]
        } else {
            [Dock::Right, Dock::Left, Dock::Top, Dock::Bottom]
        };
        // 先确定紧凑态的位置，展开时向同一方向增长，避免鼠标移入后窗口跳到球体另一侧。
        let side = preferred.into_iter().find(|side| {
            let (width, height) = capacity(*side);
            width >= base_width && height >= base_height
        });
        let (space_width, space_height) = capacity(side.unwrap_or(Dock::Free));
        let factor = if expanded {
            (space_width / base_width)
                .min(space_height / base_height)
                .clamp(1., DISPLAY_EXPANSION)
        } else {
            1.
        };
        let width = (base_width * factor).min(work.width);
        let height = (base_height * factor).min(work.height);
        let side_y = side_y(height);
        let vertical_x = self.anchor.x - width / 2.;
        let (x, y) = match side.unwrap_or(preferred[0]) {
            Dock::Left => (left_edge - gap - width, side_y),
            Dock::Right => (right_edge + gap, side_y),
            Dock::Top => (vertical_x, top_edge - gap - height),
            Dock::Bottom => (vertical_x, bottom_edge + gap),
            Dock::Free => (vertical_x, side_y),
        };
        let origin = Point {
            x: fit(x, work.origin.x, work.width, width),
            y: fit(y, work.origin.y, work.height, height),
        };
        Bounds {
            origin,
            width,
            height,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn screen(scale: f64) -> WorkArea {
        WorkArea {
            bounds: Bounds {
                origin: Point { x: -1600., y: 40. },
                width: 1600.,
                height: 860.,
            },
            scale,
        }
    }

    fn assert_close(actual: f64, expected: f64) {
        assert!((actual - expected).abs() < 1e-8, "{actual} ≈ {expected}");
    }

    #[test]
    fn corners_dock_horizontally_and_leave_room_for_the_caption() {
        let area = screen(1.5);
        let corner = area.bounds.origin;
        assert_eq!(snap(corner, area), Dock::Left);
        let placed = Placement::new(corner, Dock::Left, area);
        let caption = placed.docked_panels().unwrap().caption;
        let orb = placed.orb_bounds();
        assert_eq!(orb.origin.x, corner.x);
        assert!(orb.origin.y >= corner.y);
        assert!(orb.right() >= caption.right() && orb.bottom() >= caption.bottom());
        assert!(!pulled_inward(Dock::Left, 35., 0., 1.5));
        assert!(pulled_inward(Dock::Left, 36., 0., 1.5));
    }

    #[test]
    fn display_expands_towards_the_work_area_without_covering_the_orb() {
        let area = screen(1.5);
        for edge in [Dock::Left, Dock::Right, Dock::Top, Dock::Bottom] {
            let placed = Placement::new(area.bounds.center(), edge, area);
            let diamond = placed.diamond();
            let caption = placed.docked_panels().unwrap().caption;
            let compact = placed.display_bounds(false);
            let expanded = placed.display_bounds(true);
            assert_eq!(expanded.width, 480.);
            assert_eq!(expanded.height, 300.);
            assert!(
                expanded.origin.x >= area.bounds.origin.x
                    && expanded.right() <= area.bounds.right()
            );
            assert!(
                expanded.origin.y >= area.bounds.origin.y
                    && expanded.bottom() <= area.bounds.bottom()
            );
            match edge {
                Dock::Left => {
                    assert_close(expanded.origin.x, compact.origin.x);
                    assert_close(expanded.bottom(), compact.bottom());
                }
                Dock::Right => {
                    assert_close(expanded.right(), compact.right());
                    assert_close(expanded.bottom(), compact.bottom());
                }
                Dock::Top | Dock::Bottom => {
                    assert_eq!(expanded.right(), compact.right());
                    assert!(
                        expanded.right() < diamond.origin.x && caption.origin.x > diamond.right()
                    );
                    if edge == Dock::Top {
                        assert_eq!(expanded.origin.y, compact.origin.y);
                    } else {
                        assert_eq!(expanded.bottom(), compact.bottom());
                    }
                }
                Dock::Free => unreachable!(),
            }
        }
    }

    #[test]
    fn expansion_cannot_flip_to_the_other_side_under_the_pointer() {
        let area = WorkArea {
            bounds: Bounds {
                origin: Point { x: 0., y: 0. },
                width: 800.,
                height: 600.,
            },
            scale: 1.,
        };
        let place = Placement::new(Point { x: 430., y: 250. }, Dock::Free, area);
        let compact = place.display_bounds(false);
        let expanded = place.display_bounds(true);
        assert_eq!(compact.origin.x, expanded.origin.x);
        assert!(expanded.origin.y <= compact.origin.y && expanded.bottom() >= compact.bottom());
        assert!(expanded.width > compact.width && expanded.width < 320.);
        assert_eq!(expanded.right(), area.bounds.right());
    }

    #[test]
    fn regular_side_docks_keep_the_diagonal_midpoint_anchors() {
        let area = screen(1.25);
        let expected_distance = (ORB_MAX_OUTLINE_EDGE / 2. + PANEL_GAP) * area.scale;
        for dock in [Dock::Left, Dock::Right] {
            let beyond_corner = area.bounds.bottom() - 120. * area.scale;
            for y in [area.bounds.center().y, beyond_corner] {
                let point = Point {
                    y,
                    ..area.bounds.center()
                };
                let place = Placement::new(point, dock, area);
                let panels = place.docked_panels().unwrap();
                let side = panels.caption_side;
                assert!(panels.display_position == DisplayPosition::Beside(side));
                let caption_edge = side.near_edge(panels.caption);
                let horizontal = (caption_edge - place.anchor.x).abs();
                assert_close(panels.caption.origin.y - place.anchor.y, horizontal);
                assert_close(panels.caption.width, 126. * area.scale);
                for expanded in [false, true] {
                    let display = place.display_bounds(expanded);
                    let upward = place.anchor.y - display.bottom();
                    assert_close(side.near_edge(display), caption_edge);
                    assert_close(horizontal, upward);
                    assert_close(horizontal.hypot(upward), expected_distance);
                }
            }
        }
    }

    #[test]
    fn side_card_slides_out_when_the_top_edge_limits_the_vertical_space() {
        let area = screen(1.);
        let constrained = Placement::new(
            Point {
                x: area.bounds.origin.x,
                y: area.bounds.origin.y + DISPLAY_HEIGHT * DISPLAY_EXPANSION + SIDE_PANEL_OFFSET
                    - 10.,
            },
            Dock::Left,
            area,
        );
        let expanded = constrained.display_bounds(true);
        assert_eq!(expanded.origin.y, area.bounds.origin.y);
        assert_close(
            expanded.origin.x - constrained.anchor.x,
            SIDE_PANEL_OFFSET + 10.,
        );
        assert_close(
            expanded.origin.x,
            constrained.display_bounds(false).origin.x,
        );
    }

    #[test]
    fn upper_side_corners_stack_full_size_display_above_the_caption() {
        let area = screen(1.25);
        let gap = PANEL_GAP * area.scale;
        for dock in [Dock::Left, Dock::Right] {
            let centered = Placement::new(area.bounds.center(), dock, area);
            let near_top = Placement::new(area.bounds.origin, dock, area);
            let panels = near_top.docked_panels().unwrap();
            let side = panels.caption_side;
            assert!(panels.display_position == DisplayPosition::UpperSideStack(side));
            assert!(!centered.same_arrangement(near_top));
            let orb = near_top.orb_bounds();
            let idle = near_top.orb_frame(None).unwrap();
            assert_close(
                orb.origin.y + idle.caption_y * area.scale,
                panels.caption.origin.y,
            );
            for expanded in [false, true] {
                let display = near_top.display_bounds(expanded);
                let factor = if expanded { DISPLAY_EXPANSION } else { 1. };
                assert_close(display.origin.y, area.bounds.origin.y);
                assert_close(display.width, DISPLAY_WIDTH * factor * area.scale);
                assert_close(display.height, DISPLAY_HEIGHT * factor * area.scale);
                assert_close(
                    (side.near_edge(display) - near_top.anchor.x).abs(),
                    ORB_CLEARANCE * area.scale,
                );
                let frame = near_top.orb_frame(Some(expanded)).unwrap();
                assert_close(frame.caption_width, 126.);
                let caption_y = orb.origin.y + frame.caption_y * area.scale;
                assert_close(
                    orb.origin.x + frame.caption_x * area.scale,
                    side.near_edge(display),
                );
                assert_close(caption_y, display.bottom() + gap);
                assert!(caption_y + panels.caption.height <= orb.bottom());
            }
            let surface = near_top.display_surface();
            let compact = surface.region(false);
            let expanded = surface.region(true);
            assert!(compact.origin.x >= expanded.origin.x && compact.right() <= expanded.right());
            assert!(compact.origin.y >= expanded.origin.y && compact.bottom() <= expanded.bottom());
        }
    }

    #[test]
    fn lower_side_corners_share_the_regular_diagonal_layout() {
        let area = screen(1.25);
        for dock in [Dock::Left, Dock::Right] {
            let centered = Placement::new(area.bounds.center(), dock, area);
            let near_bottom = Placement::new(
                Point {
                    y: area.bounds.bottom(),
                    ..area.bounds.center()
                },
                dock,
                area,
            );
            let panels = near_bottom.docked_panels().unwrap();
            let side = panels.caption_side;
            let offset = SIDE_PANEL_OFFSET * area.scale;
            assert!(centered.same_arrangement(near_bottom));
            assert_close(
                (side.near_edge(panels.caption) - near_bottom.anchor.x).abs(),
                offset,
            );
            assert_close(panels.caption.origin.y, near_bottom.anchor.y + offset);
            for expanded in [false, true] {
                let display = near_bottom.display_bounds(expanded);
                let regular = centered.display_bounds(expanded);
                assert_close(
                    (side.near_edge(display) - near_bottom.anchor.x).abs(),
                    offset,
                );
                assert_close(near_bottom.anchor.y - display.bottom(), offset);
                assert_close(display.width, regular.width);
                assert_close(display.height, regular.height);
            }
        }
    }

    #[test]
    fn vertical_docking_splits_display_and_caption_around_the_diamond() {
        let area = screen(1.);
        for dock in [Dock::Top, Dock::Bottom] {
            let place = Placement::new(area.bounds.center(), dock, area);
            let caption = place.docked_panels().unwrap().caption;
            assert_eq!(caption.width, 126.);
            assert!(place.display_bounds(false).right() < place.anchor.x);
            assert!(caption.origin.x > place.anchor.x);
            assert_eq!(place.orb_frame(None).unwrap().caption_align, 0);
        }
    }

    #[test]
    fn vertical_corners_center_the_display_as_far_as_the_screen_allows() {
        let area = screen(1.25);
        let work = area.bounds;
        let gap = PANEL_GAP * area.scale;
        for dock in [Dock::Top, Dock::Bottom] {
            let center = Placement::new(work.center(), dock, area);
            for (x, expected_side, fits_centered) in [
                (work.origin.x, Side::Right, false),
                (work.right(), Side::Left, false),
                (work.origin.x + 235. * area.scale, Side::Right, true),
                (work.right() - 175. * area.scale, Side::Left, true),
            ] {
                let place = Placement::new(
                    Point {
                        x,
                        y: work.origin.y,
                    },
                    dock,
                    area,
                );
                let panels = place.docked_panels().unwrap();
                assert!(panels.caption_side == expected_side);
                assert!(panels.display_position == DisplayPosition::CenteredStack);
                assert!(!center.same_arrangement(place));
                let diamond = place.diamond();
                for expanded in [false, true] {
                    let display = place.display_bounds(expanded);
                    if fits_centered {
                        assert_close(display.center().x, place.anchor.x);
                    } else {
                        match expected_side {
                            Side::Left => assert_close(display.right(), work.right() - gap),
                            Side::Right => assert_close(display.origin.x, work.origin.x + gap),
                        }
                    }
                    if dock == Dock::Top {
                        assert_close(display.origin.y, diamond.bottom() + gap);
                        assert!(display.origin.y > panels.caption.bottom());
                    } else {
                        assert_close(display.bottom(), diamond.origin.y - gap);
                        assert!(display.bottom() < panels.caption.origin.y);
                    }
                    assert!(display.origin.x >= work.origin.x && display.right() <= work.right());
                    assert!(display.origin.y >= work.origin.y && display.bottom() <= work.bottom());
                }
            }
        }
    }

    #[test]
    fn both_hover_states_fit_in_one_canvas_and_keep_the_old_hit_area() {
        let area = screen(1.5);
        for dock in [Dock::Free, Dock::Left, Dock::Right, Dock::Top, Dock::Bottom] {
            let place = Placement::new(area.bounds.center(), dock, area);
            let surface = place.display_surface();
            let compact = surface.region(false);
            let expanded = surface.region(true);
            assert_eq!(surface.frame(false).scale, 1.);
            assert_eq!(surface.frame(true).scale, 1.25);
            assert!(compact.origin.x >= 0. && compact.origin.y >= 0.);
            assert!(compact.right() <= expanded.right() && compact.bottom() <= expanded.bottom());
        }
    }

    #[test]
    fn fractional_scale_leaves_room_for_antialiased_edges_on_both_card_sizes() {
        for scale in [1., 1.25, 1.5] {
            let area = screen(scale);
            let place = Placement::new(
                Point {
                    x: -417.4,
                    y: 381.6,
                },
                Dock::Right,
                area,
            );
            let surface = place.display_surface();
            for focused in [false, true] {
                let paint = surface.region(focused);
                let clip = paint.enclosing_pixels(DISPLAY_CLIP_MARGIN);
                assert!(paint.origin.x - clip.origin.x >= DISPLAY_CLIP_MARGIN);
                assert!(paint.origin.y - clip.origin.y >= DISPLAY_CLIP_MARGIN);
                assert!(clip.right() - paint.right() >= DISPLAY_CLIP_MARGIN);
                assert!(clip.bottom() - paint.bottom() >= DISPLAY_CLIP_MARGIN);
                assert!(clip.origin.x >= 0. && clip.origin.y >= 0.);
                assert!(
                    clip.right() <= surface.canvas.width && clip.bottom() <= surface.canvas.height
                );
                assert_eq!(
                    surface.frame(focused).scale,
                    if focused { 1.25 } else { 1. }
                );
            }
        }
    }
}
