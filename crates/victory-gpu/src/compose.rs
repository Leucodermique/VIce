//! Two presented planes as one image: the console's display puts the second
//! (the interface) over the first (the scene), each scaled to the screen.
//! Grand Theft Auto VI presents so on Series S.
//!
//! VIce enhancements on top of the two-plane compositor:
//! - Optional lightweight 5-tap Contrast Adaptive Sharpening (`VICTORY_SHARPEN`)
//!   applied to the 3D scene plane only (never blurring or ringing the HUD).
//! - Real-time Xbox -> PC keycap/mouse prompt icon replacement (`VICTORY_PC_PROMPTS=1`)
//!   on the UI plane when playing with keyboard & mouse (auto-disables when any
//!   XInput or PS5 DualSense controller is connected), using a 1280x128 8x-supersampled
//!   R8G8_UNORM atlas, ZNCC 5x5 glyph classification, smooth native alpha fade-in/out,
//!   outer drop-shadow halo for bright skies, hold-ring trigger & button support,
//!   anti-flip ID stabilization, and a full-screen loading/menu opaque guard.

use std::sync::atomic::{AtomicBool, Ordering};

use windows::Win32::Graphics::Direct3D::Fxc::D3DCompile;
use windows::Win32::Graphics::Direct3D::ID3DBlob;
use windows::Win32::Graphics::Direct3D12::*;
use windows::Win32::Graphics::Dxgi::Common::*;
use windows::core::{PCSTR, s};

use crate::host::Host;

/// Embedded 1280x128 R8G8_UNORM alpha+ink atlas (10 crisp 128x128 GTA VI-style PC prompt icons):
/// R = alpha coverage (0 outside silhouette -> guaranteed zero black box),
/// G = ink luminance (0 = charcoal outline/letter, 1 = pure white keycap / active mouse button).
/// 0: RMB (LT / R-stick), 1: LMB (RT), 2: TAB (LB), 3: G (RB), 4: MAJ (colored A),
/// 5: R (B), 6: SPACE (X), 7: F (Y), 8: E (Interact/Talk A), 9: C (RS).
const ATLAS_W: u32 = 1280;
const ATLAS_H: u32 = 128;
const ATLAS_BPP: u32 = 2;
const ATLAS_BYTES: &[u8] = include_bytes!("pc_prompts_atlas.bin");
const _: () = assert!(ATLAS_BYTES.len() == (ATLAS_W * ATLAS_H * ATLAS_BPP) as usize);

/// Updated by `victory_hle::modules::gameinput`: when a real XInput or PS5 DualSense
/// gamepad is connected, keep the original console button icons automatically.
static REAL_PAD_CONNECTED: AtomicBool = AtomicBool::new(false);

pub fn set_real_pad_connected(connected: bool) {
    REAL_PAD_CONNECTED.store(connected, Ordering::Relaxed);
}

fn sharpen_amount() -> f32 {
    static AMT: std::sync::OnceLock<f32> = std::sync::OnceLock::new();
    *AMT.get_or_init(|| {
        std::env::var("VICTORY_SHARPEN")
            .ok()
            .and_then(|v| v.trim().parse::<f32>().ok())
            .map(|v| v.clamp(0.0, 1.0))
            .unwrap_or(0.0)
    })
}

fn pc_prompts_enabled() -> bool {
    static ON: std::sync::OnceLock<bool> = std::sync::OnceLock::new();
    *ON.get_or_init(|| std::env::var_os("VICTORY_PC_PROMPTS").is_some_and(|v| v != "0"))
}

/// Compute shaders for real-time Xbox prompt detection & temporal stabilization on the UI plane.
const DETECT_SOURCE: &str = r#"
#define RS "RootConstants(num32BitConstants=2, b0), DescriptorTable(SRV(t0, numDescriptors=1), UAV(u0, numDescriptors=1)), StaticSampler(s0, filter=FILTER_MIN_MAG_MIP_LINEAR, addressU=TEXTURE_ADDRESS_CLAMP, addressV=TEXTURE_ADDRESS_CLAMP)"

cbuffer Params : register(b0) {
    uint w;
    uint h;
};

Texture2D<float4> ui : register(t0);
// Prompt detection state (raw u32 buffer, 512 dwords = 2 KiB):
// dword 0: raw candidate count (0..64)
// dwords 1..1+64*4: raw candidates (cx, cy, packed_icon_style_alpha, score_u32)
//   packed: bits 0..7 = icon_id (0..9), bits 8..15 = style (0=box popup, 1=floating circle, 2=ring inner, 3=dimmed circle),
//           bits 16..23 = prompt_alpha_u8 (0..255)
// dword 260: merged hit count (0..8)
// dwords 261..261+8*8: merged hits: box(cx, cy, hw, hh) + meta(icon, style, alpha_u8, reserved)
// dword 320: persistent hit count (0..8)
// dwords 321..321+8*8: persistent hits: box(cx, cy, hw, hh) + meta(icon, style, ttl, hold_conf)
RWByteAddressBuffer prompts : register(u0);
SamplerState linear_clamp : register(s0);

static const float3 LUMA = float3(0.299, 0.587, 0.114);
static const float TWO_PI = 6.28318530718;

float4 sample_ui_px(float2 px) {
    float2 uv = (px + 0.5) / float2(w, h);
    float4 c = ui.SampleLevel(linear_clamp, uv, 0);
    // Un-premultiply safely when alpha > 0 so color thresholds work on premultiplied planes too.
    float3 rgb = (c.a > 0.02) ? saturate(c.rgb / max(c.a, 0.02)) : float3(0.0, 0.0, 0.0);
    return float4(rgb, saturate(c.a));
}

bool is_popup_bg(float4 c) {
    float l = dot(c.rgb, LUMA);
    float hi = max(c.r, max(c.g, c.b));
    float lo = min(c.r, min(c.g, c.b));
    return c.a > 0.50 && l < 0.25 && (hi - lo) < 0.09;
}

bool is_white_glyph(float4 c) {
    float l = dot(c.rgb, LUMA);
    float hi = max(c.r, max(c.g, c.b));
    float lo = min(c.r, min(c.g, c.b));
    return c.a > 0.45 && l > 0.68 && (hi - lo) < 0.22;
}

bool is_prompt_disc(float4 c) {
    float l = dot(c.rgb, LUMA);
    float hi = max(c.r, max(c.g, c.b));
    float lo = min(c.r, min(c.g, c.b));
    return c.a > 0.40 && l > 0.34 && (hi - lo) < 0.17;
}

bool is_dark_ink(float4 c) {
    float l = dot(c.rgb, LUMA);
    return c.a > 0.35 && l < 0.34;
}

// 4: A (green), 5: B (red), 6: X (blue), 7: Y (yellow), -1: none
int classify_abxy_color(float4 c) {
    if (c.a < 0.45) return -1;
    if (c.g > 0.42 && (c.g - c.r) > 0.15 && (c.g - c.b) > 0.15) return 4;
    if (c.r > 0.52 && (c.r - c.g) > 0.20 && (c.r - c.b) > 0.20) return 5;
    if (c.b > 0.48 && (c.b - c.r) > 0.18 && c.g > 0.18) return 6;
    if (c.r > 0.62 && c.g > 0.50 && c.b < 0.30 && (c.r - c.b) > 0.38) return 7;
    return -1;
}

// Classifies the dark letter inside GTA VI's monochrome white circular button
// using translation-invariant ink-bbox recentering + 5x5 ZNCC:
// A -> E (tile 8), X -> SPACE (tile 6), R (right stick) -> Mouse RMB (tile 0),
// B -> R (tile 5), Y -> F (tile 7).
int classify_white_circle_letter(float2 p, float scale) {
    float minx = 100.0, maxx = -100.0, miny = 100.0, maxy = -100.0;
    uint found = 0u;
    for (int j = -6; j <= 6; j++) {
        for (int i = -6; i <= 6; i++) {
            if (i * i + j * j > 38) continue;
            float4 s = sample_ui_px(p + float2(float(i), float(j)) * scale);
            float l = dot(s.rgb, LUMA);
            if (s.a > 0.35 && l < 0.34) {
                found++;
                minx = min(minx, float(i));
                maxx = max(maxx, float(i));
                miny = min(miny, float(j));
                maxy = max(maxy, float(j));
            }
        }
    }
    if (found < 2u) return 8;

    float2 g = p + float2(0.5 * (minx + maxx), 0.5 * (miny + maxy)) * scale;
    float hx = max(2.8, 0.45 * (maxx - minx)) * scale;
    float hy = max(3.2, 0.45 * (maxy - miny)) * scale;

    float vec[25];
    float mean = 0.0;
    for (int j = 0; j < 5; j++) {
        for (int i = 0; i < 5; i++) {
            float u = -1.0 + 0.5 * float(i);
            float v = -1.0 + 0.5 * float(j);
            float4 s = sample_ui_px(g + float2(u * hx, v * hy));
            float l = dot(s.rgb, LUMA);
            float dark = saturate((0.75 - l) / 0.55);
            vec[j * 5 + i] = dark;
            mean += dark;
        }
    }
    mean /= 25.0;
    for (int k = 0; k < 25; k++) vec[k] -= mean;

    static const uint MASKS[5] = {
        0x11FA944u, // A -> 8 (E)
        0x1151151u, // X -> 6 (SPACE)
        0x0929D2Fu, // R -> 0 (MOUSE)
        0x0F4BD2Fu, // B -> 5 (R)
        0x0421151u  // Y -> 7 (F)
    };
    static const int IDS[5] = { 8, 6, 0, 5, 7 };

    int best_id = 8;
    float best_sc = -1e9;
    for (int t = 0; t < 5; t++) {
        uint mask = MASKS[t];
        float ones = 0.0;
        for (int k = 0; k < 25; k++) {
            if (((mask >> uint(k)) & 1u) != 0u) ones += 1.0;
        }
        float tm = ones / 25.0;
        float dotv = 0.0;
        float norm2 = 0.0;
        for (int k = 0; k < 25; k++) {
            float tv = (((mask >> uint(k)) & 1u) != 0u ? 1.0 : 0.0) - tm;
            dotv += vec[k] * tv;
            norm2 += tv * tv;
        }
        float sc = dotv / sqrt(max(norm2, 1e-4));
        if (sc > best_sc) {
            best_sc = sc;
            best_id = IDS[t];
        }
    }
    return best_id;
}

void push_candidate(float2 p, uint icon, uint style, float alpha, float score) {
    uint slot;
    prompts.InterlockedAdd(0, 1, slot);
    if (slot < 64u) {
        uint base = (1u + slot * 4u) * 4u;
        uint a8 = uint(saturate(alpha) * 255.0 + 0.5);
        uint packed = (icon & 0xFFu) | ((style & 0xFFu) << 8) | ((a8 & 0xFFu) << 16);
        prompts.Store4(base, uint4(uint(p.x), uint(p.y), packed, uint(score * 1000.0)));
    }
}

#ifdef CLEAR_HITS
[RootSignature(RS)]
[numthreads(1, 1, 1)]
void clear_hits() {
    prompts.Store(0, 0);
    prompts.Store(260 * 4, 0);
}
#endif

#ifdef DETECT_HITS
[RootSignature(RS)]
[numthreads(8, 8, 1)]
void detect_hits(uint3 tid : SV_DispatchThreadID) {
    float scale = float(h) / 1080.0;
    float2 p = float2(tid.xy * 2u + 1u);
    if (p.x < 26.0 * scale || p.x >= float(w) - 26.0 * scale ||
        p.y < 26.0 * scale || p.y >= float(h) - 26.0 * scale) {
        return;
    }

    // Global full-screen UI guard: loading artwork or pause menus cover the center opaquely.
    uint opaque_probes = 0u;
    if (ui.SampleLevel(linear_clamp, float2(0.50, 0.30), 0).a > 0.40) opaque_probes++;
    if (ui.SampleLevel(linear_clamp, float2(0.50, 0.50), 0).a > 0.40) opaque_probes++;
    if (ui.SampleLevel(linear_clamp, float2(0.35, 0.45), 0).a > 0.40) opaque_probes++;
    if (ui.SampleLevel(linear_clamp, float2(0.65, 0.45), 0).a > 0.40) opaque_probes++;
    if (ui.SampleLevel(linear_clamp, float2(0.50, 0.65), 0).a > 0.40) opaque_probes++;
    if (opaque_probes >= 3u) {
        return;
    }

    // Exclude bottom-left minimap/radar region to guarantee zero false positives.
    if (p.x < float(w) * 0.24 && p.y > float(h) * 0.68) {
        return;
    }

    float4 center = sample_ui_px(p);

    // ---- MODE A: Floating interaction & objective prompts (no dark box) ----
    bool in_bottom_right = (p.x > float(w) * 0.70 && p.y > float(h) * 0.52);
    bool in_bottom_center = (p.x > float(w) * 0.24 && p.x < float(w) * 0.78 && p.y > float(h) * 0.82);
    if (in_bottom_right || in_bottom_center) {
        // A1) Hold-ring prompt (bottom-right): crisp white ring at radius ~18px, transparent outside at ~25px.
        // Supports BOTH hold triggers (LT/RT -> RMB/LMB) AND hold circular face buttons (A->E, X->SPACE, B->R, Y->F).
        if (in_bottom_right) {
            bool outer_clear =
                sample_ui_px(p + float2(0.0, -25.0 * scale)).a < 0.18 &&
                sample_ui_px(p + float2(0.0,  25.0 * scale)).a < 0.18 &&
                sample_ui_px(p + float2(25.0 * scale, 0.0)).a < 0.18;
            if (outer_clear) {
                uint ring18 = 0u;
                uint ring25_clear = 0u;
                float peak_a = 0.0;
                for (uint k = 0u; k < 16u; k++) {
                    float ang = float(k) * (TWO_PI / 16.0);
                    float2 dir = float2(cos(ang), sin(ang));
                    float4 r18 = sample_ui_px(p + dir * (18.0 * scale));
                    if (is_white_glyph(r18)) {
                        ring18++;
                        peak_a = max(peak_a, r18.a);
                    }
                    if (sample_ui_px(p + dir * (25.0 * scale)).a < 0.18) ring25_clear++;
                }
                if (ring18 >= 12u && ring25_clear >= 10u) {
                    float inner_w = 0.0;
                    uint inner_d = 0u;
                    float tl = 0.0, tr = 0.0, bl = 0.0, br = 0.0;
                    for (int j = 0; j < 6; j++) {
                        for (int i = 0; i < 6; i++) {
                            float2 off = float2(-9.0 + float(i) * (18.0 / 5.0), -9.0 + float(j) * (18.0 / 5.0)) * scale;
                            float4 s = sample_ui_px(p + off);
                            float v = is_white_glyph(s) ? 1.0 : 0.0;
                            inner_w += v;
                            if (i >= 1 && i <= 4 && j >= 1 && j <= 4 && is_dark_ink(s)) inner_d++;
                            if (j < 2 && i < 2) tl += v;
                            if (j < 2 && i >= 4) tr += v;
                            if (j >= 4 && i < 2) bl += v;
                            if (j >= 4 && i >= 4) br += v;
                        }
                    }
                    float fill = inner_w / 36.0;
                    if (fill >= 0.40 && inner_d >= 2u) {
                        uint icon;
                        // Asymmetric bottom corner -> trigger silhouette (LT / RT).
                        if (abs(bl - br) >= 0.9 && (tl + tr) >= 2.5) {
                            icon = (br > bl) ? 1u : 0u; // LT -> RMB (0), RT -> LMB (1)
                        } else {
                            // Symmetric circular disc inside hold ring -> classify letter (A->E, X->SPACE, B->R, Y->F).
                            icon = uint(classify_white_circle_letter(p, scale));
                        }
                        push_candidate(p, icon, 2u, peak_a, fill);
                        return;
                    }
                }
            }
        }

        // A2) Floating LT/RT trigger in bottom-center subtitle prompts
        if (in_bottom_center && center.a > 0.42) {
            float pad_f = 18.0 * scale;
            bool clear_top_bot = sample_ui_px(p + float2(0.0, -pad_f)).a < 0.15
                              && sample_ui_px(p + float2(0.0,  pad_f)).a < 0.15;
            if (clear_top_bot) {
                float half_f = 11.0 * scale;
                float total = 0.0, top_bar = 0.0, bot_bar = 0.0, peak_a = center.a;
                uint inner_d = 0u;
                float tl = 0.0, tr = 0.0, bl = 0.0, br = 0.0;
                for (int j = 0; j < 8; j++) {
                    for (int i = 0; i < 8; i++) {
                        float2 off = float2(-half_f + float(i) * (2.0 * half_f / 7.0),
                                            -half_f + float(j) * (2.0 * half_f / 7.0));
                        float4 s = sample_ui_px(p + off);
                        float v = is_white_glyph(s) ? 1.0 : 0.0;
                        if (v > 0.5) peak_a = max(peak_a, s.a);
                        total += v;
                        if (i >= 2 && i <= 5 && j >= 2 && j <= 5 && is_dark_ink(s)) inner_d++;
                        if (j < 2 && i >= 1 && i <= 6) top_bar += v;
                        if (j >= 6 && i >= 1 && i <= 6) bot_bar += v;
                        if (j < 2 && i < 2) tl += v;
                        if (j < 2 && i >= 6) tr += v;
                        if (j >= 6 && i < 2) bl += v;
                        if (j >= 6 && i >= 6) br += v;
                    }
                }
                float fill = total / 64.0;
                if (fill >= 0.46 && inner_d >= 2u && top_bar / 12.0 >= 0.58 && bot_bar / 12.0 >= 0.48) {
                    tl *= 0.25; tr *= 0.25; bl *= 0.25; br *= 0.25;
                    if (tl >= 0.45 && tr >= 0.45 && bl >= 0.45 && br <= 0.35 && (bl - br) >= 0.25) {
                        push_candidate(p, 0u, 1u, peak_a, fill);
                        return;
                    }
                    if (tl >= 0.45 && tr >= 0.45 && br >= 0.45 && bl <= 0.35 && (br - bl) >= 0.25) {
                        push_candidate(p, 1u, 1u, peak_a, fill);
                        return;
                    }
                }
            }
        }

        // A3) Monochrome white or dimmed-grey circular button (PARLER (A), MENACER (X), (B), (Y), (R)):
        bool vert_clear =
            sample_ui_px(p + float2(0.0, -17.0 * scale)).a < 0.18 &&
            sample_ui_px(p + float2(0.0,  17.0 * scale)).a < 0.18;
        if (vert_clear) {
            float d_off = 15.0 * scale;
            uint diag_clear = 0u;
            if (sample_ui_px(p + float2(-d_off, -d_off)).a < 0.18) diag_clear++;
            if (sample_ui_px(p + float2( d_off, -d_off)).a < 0.18) diag_clear++;
            if (sample_ui_px(p + float2(-d_off,  d_off)).a < 0.18) diag_clear++;
            if (sample_ui_px(p + float2( d_off,  d_off)).a < 0.18) diag_clear++;
            if (diag_clear >= 3u) {
                uint ring10 = 0u;
                float ring_luma = 0.0;
                float peak_a = 0.0;
                for (uint k = 0u; k < 12u; k++) {
                    float ang = float(k) * (TWO_PI / 12.0);
                    float2 dir = float2(cos(ang), sin(ang));
                    float4 rs = sample_ui_px(p + dir * (10.0 * scale));
                    if (is_prompt_disc(rs)) {
                        ring10++;
                        ring_luma += dot(rs.rgb, LUMA);
                        peak_a = max(peak_a, rs.a);
                    }
                }
                if (ring10 >= 10u) {
                    uint dark_cnt = 0u;
                    for (int dy = -1; dy <= 1; dy++) {
                        for (int dx = -1; dx <= 1; dx++) {
                            if (is_dark_ink(sample_ui_px(p + float2(float(dx) * 4.0, float(dy) * 4.0) * scale))) {
                                dark_cnt++;
                            }
                        }
                    }
                    if (dark_cnt >= 2u && dark_cnt <= 7u) {
                        int icon = classify_white_circle_letter(p, scale);
                        uint style = (ring_luma / float(ring10) < 0.65) ? 3u : 1u;
                        push_candidate(p, uint(icon), style, peak_a, float(ring10) / 12.0);
                        return;
                    }
                }
            }
        }
    }

    // ---- MODE B: Dark pop-up box prompts (top-left & top-right hints) ----
    bool in_top_box = (p.y <= float(h) * 0.44) && (p.x <= float(w) * 0.52 || p.x >= float(w) * 0.56);
    if (!in_top_box || center.a < 0.45) return;

    float pad = 17.0 * scale;
    float4 top_bg = sample_ui_px(p + float2(0.0, -pad));
    float4 bot_bg = sample_ui_px(p + float2(0.0,  pad));
    float4 l_bg   = sample_ui_px(p + float2(-pad, 0.0));
    float4 r_bg   = sample_ui_px(p + float2( pad, 0.0));
    if (!is_popup_bg(top_bg) || !is_popup_bg(bot_bg)) return;
    if (!is_popup_bg(l_bg) || !is_popup_bg(r_bg)) return;

    float l_top = dot(top_bg.rgb, LUMA);
    float l_bot = dot(bot_bg.rgb, LUMA);
    float l_l   = dot(l_bg.rgb, LUMA);
    float l_r   = dot(r_bg.rgb, LUMA);
    if (abs(l_top - l_bot) > 0.06 || abs(l_l - l_r) > 0.06) return;
    bool outside_clear =
        sample_ui_px(p + float2(0.0, -48.0 * scale)).a < 0.18 ||
        sample_ui_px(p + float2(0.0,  48.0 * scale)).a < 0.18;
    if (!outside_clear) return;

    // B1) Colored A/B/X/Y circle buttons inside pop-up boxes.
    int col_icon = classify_abxy_color(center);
    if (col_icon >= 0) {
        float c_pad = 14.0 * scale;
        if (!is_popup_bg(sample_ui_px(p + float2(-c_pad, -c_pad))) ||
            !is_popup_bg(sample_ui_px(p + float2( c_pad, -c_pad))) ||
            !is_popup_bg(sample_ui_px(p + float2(-c_pad,  c_pad))) ||
            !is_popup_bg(sample_ui_px(p + float2( c_pad,  c_pad)))) {
            return;
        }
        float match_cnt = 0.0;
        float half_box = 10.5 * scale;
        float peak_a = center.a;
        for (int j = 0; j < 6; j++) {
            for (int i = 0; i < 6; i++) {
                float2 off = float2(-half_box + float(i) * (2.0 * half_box / 5.0),
                                    -half_box + float(j) * (2.0 * half_box / 5.0));
                float4 s = sample_ui_px(p + off);
                if (classify_abxy_color(s) == col_icon) {
                    match_cnt += 1.0;
                    peak_a = max(peak_a, s.a);
                }
            }
        }
        float ratio = match_cnt / 36.0;
        if (ratio >= 0.42) {
            push_candidate(p, uint(col_icon), 0u, peak_a, ratio);
        }
        return;
    }

    // B2) Solid white trigger/bumper silhouettes (LT, RT, LB, RB) with dark inner letters.
    float half_w = 11.5 * scale;
    float half_h = 11.5 * scale;
    float total = 0.0;
    uint inner_dark = 0u;
    float top_bar = 0.0, bot_bar = 0.0;
    float tl = 0.0, tr = 0.0, bl = 0.0, br = 0.0;
    float peak_a = center.a;

    for (int j = 0; j < 8; j++) {
        for (int i = 0; i < 8; i++) {
            float2 off = float2(-half_w + float(i) * (2.0 * half_w / 7.0),
                                -half_h + float(j) * (2.0 * half_h / 7.0));
            float4 s = sample_ui_px(p + off);
            float v = is_white_glyph(s) ? 1.0 : 0.0;
            if (v > 0.5) peak_a = max(peak_a, s.a);
            total += v;
            if (i >= 2 && i <= 5 && j >= 2 && j <= 5 && is_dark_ink(s)) inner_dark++;
            if (j < 2 && i >= 1 && i <= 6) top_bar += v;
            if (j >= 6 && i >= 1 && i <= 6) bot_bar += v;
            if (j < 2 && i < 2) tl += v;
            if (j < 2 && i >= 6) tr += v;
            if (j >= 6 && i < 2) bl += v;
            if (j >= 6 && i >= 6) br += v;
        }
    }

    float fill = total / 64.0;
    if (fill < 0.46 || inner_dark < 2u) return;
    if (top_bar / 12.0 < 0.58 || bot_bar / 12.0 < 0.48) return;

    tl *= 0.25; tr *= 0.25; bl *= 0.25; br *= 0.25;

    int icon = -1;
    if (tl >= 0.45 && tr >= 0.45 && bl >= 0.45 && br <= 0.35 && (bl - br) >= 0.25) icon = 0; // LT -> RMB
    else if (tl >= 0.45 && tr >= 0.45 && br >= 0.45 && bl <= 0.35 && (br - bl) >= 0.25) icon = 1; // RT -> LMB
    else if (bl >= 0.45 && br >= 0.45 && tr >= 0.45 && tl <= 0.35 && (tr - tl) >= 0.22) icon = 2; // LB -> TAB
    else if (bl >= 0.45 && br >= 0.45 && tl >= 0.45 && tr <= 0.35 && (tl - tr) >= 0.22) icon = 3; // RB -> G

    if (icon >= 0) {
        push_candidate(p, uint(icon), 0u, peak_a, fill);
    }
}
#endif

#ifdef MERGE_HITS
[RootSignature(RS)]
[numthreads(1, 1, 1)]
void merge_hits() {
    float scale = float(h) / 1080.0;

    uint opaque_probes = 0u;
    if (ui.SampleLevel(linear_clamp, float2(0.50, 0.30), 0).a > 0.40) opaque_probes++;
    if (ui.SampleLevel(linear_clamp, float2(0.50, 0.50), 0).a > 0.40) opaque_probes++;
    if (ui.SampleLevel(linear_clamp, float2(0.35, 0.45), 0).a > 0.40) opaque_probes++;
    if (ui.SampleLevel(linear_clamp, float2(0.65, 0.45), 0).a > 0.40) opaque_probes++;
    if (ui.SampleLevel(linear_clamp, float2(0.50, 0.65), 0).a > 0.40) opaque_probes++;
    if (opaque_probes >= 3u) {
        prompts.Store(260 * 4, 0u);
        prompts.Store(320 * 4, 0u);
        return;
    }

    // Load previous frame's persistent hits (dwords 320..385) for anti-flicker & ID lock.
    uint prev_n = min(prompts.Load(320 * 4), 8u);
    uint4 prev_box[8];
    uint4 prev_meta[8]; // (icon, style, ttl, alpha_u8)
    bool prev_matched[8];
    for (uint p = 0u; p < 8u; p++) {
        prev_matched[p] = false;
        if (p < prev_n) {
            uint psrc = (321u + p * 8u) * 4u;
            prev_box[p] = prompts.Load4(psrc);
            prev_meta[p] = prompts.Load4(psrc + 16u);
        } else {
            prev_box[p] = uint4(0, 0, 0, 0);
            prev_meta[p] = uint4(0, 0, 0, 0);
        }
    }

    uint raw_n = min(prompts.Load(0), 64u);
    float merge_r = 22.0 * scale;

    bool used[64];
    [unroll]
    for (uint k = 0u; k < 64u; k++) used[k] = false;

    uint out_n = 0u;
    uint4 out_box[8];
    uint4 out_meta[8]; // (icon, style, ttl, alpha_u8)

    for (uint i = 0u; i < raw_n && out_n < 8u; i++) {
        if (used[i]) continue;
        uint4 a = prompts.Load4((1u + i * 4u) * 4u);
        float sw = float(a.w);
        float sx = float(a.x) * sw;
        float sy = float(a.y) * sw;
        uint best_w = a.w;
        uint best_z = a.z;
        used[i] = true;
        for (uint j = i + 1u; j < raw_n; j++) {
            if (used[j]) continue;
            uint4 b = prompts.Load4((1u + j * 4u) * 4u);
            float2 d = float2(float(b.x) - float(a.x), float(b.y) - float(a.y));
            if (dot(d, d) <= merge_r * merge_r) {
                float bw = float(b.w);
                sx += float(b.x) * bw;
                sy += float(b.y) * bw;
                sw += bw;
                if (b.w > best_w) {
                    best_w = b.w;
                    best_z = b.z;
                }
                used[j] = true;
            }
        }
        float cx = sx / max(sw, 1.0);
        float cy = sy / max(sw, 1.0);
        uint icon = best_z & 0xFFu;
        uint style = (best_z >> 8) & 0xFFu;
        uint a8 = (best_z >> 16) & 0xFFu;

        // Match against previous frame's hits within 18px to stabilize position & prevent single-frame icon flips.
        float match_r = 18.0 * scale;
        for (uint p = 0u; p < prev_n; p++) {
            if (prev_matched[p]) continue;
            float2 pd = float2(cx - float(prev_box[p].x), cy - float(prev_box[p].y));
            if (dot(pd, pd) <= match_r * match_r) {
                prev_matched[p] = true;
                cx = lerp(float(prev_box[p].x), cx, 0.50);
                cy = lerp(float(prev_box[p].y), cy, 0.50);
                // Keep previous icon ID when score is marginal (< 0.88) to stop E <-> SPACE jitter during pulse animations.
                if (prev_meta[p].x != icon && best_w < 880u && prev_meta[p].z >= 6u) {
                    icon = prev_meta[p].x;
                }
                // Smooth alpha fade transition.
                a8 = uint(lerp(float(prev_meta[p].w), float(a8), 0.65) + 0.5);
                break;
            }
        }

        float rad = (style == 2u) ? 14.5 : 15.5;
        uint hw = uint(rad * scale + 0.5);
        uint hh = uint(rad * scale + 0.5);
        out_box[out_n] = uint4(uint(cx + 0.5), uint(cy + 0.5), hw, hh);
        out_meta[out_n] = uint4(icon, style, 10u, clamp(a8, 32u, 255u));
        out_n++;
    }

    // Carry over unmatched previous hits for up to 10 frames as long as UI pixels stay visible there,
    // modulated by the live UI alpha at that location so fade-outs look 100% native.
    for (uint p = 0u; p < prev_n && out_n < 8u; p++) {
        if (prev_matched[p] || prev_meta[p].z <= 1u) continue;
        float2 pc = float2(prev_box[p].xy);
        uint pstyle = prev_meta[p].y;
        float a_center = ui.SampleLevel(linear_clamp, (pc + 0.5) / float2(w, h), 0).a;
        float a_ring = ui.SampleLevel(linear_clamp, (pc + float2((pstyle == 2u ? 18.0 : 9.0) * scale, 0.0) + 0.5) / float2(w, h), 0).a;
        float live_a = max(a_center, a_ring);
        if (live_a > 0.22) {
            uint a8 = uint(saturate(live_a) * 255.0 + 0.5);
            out_box[out_n] = prev_box[p];
            out_meta[out_n] = uint4(prev_meta[p].x, pstyle, prev_meta[p].z - 1u, a8);
            out_n++;
        }
    }

    prompts.Store(260 * 4, out_n);
    prompts.Store(320 * 4, out_n);
    for (uint k = 0u; k < out_n; k++) {
        uint dst = (261u + k * 8u) * 4u;
        prompts.Store4(dst, out_box[k]);
        prompts.Store4(dst + 16u, out_meta[k]);

        uint pdst = (321u + k * 8u) * 4u;
        prompts.Store4(pdst, out_box[k]);
        prompts.Store4(pdst + 16u, out_meta[k]);
    }
}
#endif
"#;

/// Full-screen triangle graphics shader: composites UI over scene with optional
/// lightweight 5-tap CAS sharpening on scene + ultra-crisp 1280x128 PC prompt icons.
const GRAPHICS_SOURCE: &str = r#"
#define RS "RootConstants(num32BitConstants=4, b0, visibility=SHADER_VISIBILITY_PIXEL), DescriptorTable(SRV(t0, numDescriptors=4), visibility=SHADER_VISIBILITY_PIXEL), StaticSampler(s0, filter=FILTER_MIN_MAG_MIP_LINEAR, addressU=TEXTURE_ADDRESS_CLAMP, addressV=TEXTURE_ADDRESS_CLAMP)"

cbuffer Params : register(b0) {
    uint w;
    uint h;
    float sharpen;
    uint pc_prompts;
};

Texture2D<float4> scene : register(t0);
Texture2D<float4> over : register(t1);
Texture2D<float2> atlas : register(t2);
ByteAddressBuffer prompts : register(t3);
SamplerState smooth_ : register(s0);

static const float3 LUMA = float3(0.299, 0.587, 0.114);

struct V { float4 at : SV_Position; float2 uv : TEXCOORD0; };

[RootSignature(RS)]
V vs(uint id : SV_VertexID) {
    V v;
    v.uv = float2((id << 1) & 2, id & 2);
    v.at = float4(v.uv * float2(2, -2) + float2(-1, 1), 0, 1);
    return v;
}

float3 sample_scene_cas(float2 uv) {
    float3 c = scene.SampleLevel(smooth_, uv, 0).rgb;
    if (sharpen <= 0.001 || w < 320u || h < 240u) return c;
    float2 px = 1.0 / float2(w, h);
    float3 n = scene.SampleLevel(smooth_, uv + float2( 0.0, -px.y), 0).rgb;
    float3 s = scene.SampleLevel(smooth_, uv + float2( 0.0,  px.y), 0).rgb;
    float3 e = scene.SampleLevel(smooth_, uv + float2( px.x,  0.0), 0).rgb;
    float3 b = scene.SampleLevel(smooth_, uv + float2(-px.x,  0.0), 0).rgb;

    float3 mn = min(c, min(min(n, s), min(e, b)));
    float3 mx = max(c, max(max(n, s), max(e, b)));
    float3 amp = saturate(min(mn, 1.0 - mx) / max(mx, 1e-4));
    float3 weight = sqrt(amp) * (-0.16 * sharpen);
    return saturate((c + (n + s + e + b) * weight) / (1.0 + 4.0 * weight));
}

[RootSignature(RS)]
float4 ps(V v) : SV_Target {
    float3 s = sample_scene_cas(v.uv);
    float4 o = over.SampleLevel(smooth_, v.uv, 0);

    if (pc_prompts != 0u && w >= 320u && h >= 240u) {
        float2 px = v.uv * float2(w, h);
        float scale = float(h) / 1080.0;
        uint n = min(prompts.Load(260 * 4), 8u);
        for (uint k = 0u; k < n; k++) {
            uint src = (261u + k * 8u) * 4u;
            uint4 box = prompts.Load4(src);
            uint4 meta = prompts.Load4(src + 16u);
            float2 cpos = float2(box.xy);
            float2 half_sz = float2(box.zw);
            uint icon = meta.x;
            uint style = meta.y;
            float prompt_a = saturate(float(meta.w) / 255.0);

            float2 rel = (px - cpos) / max(half_sz, float2(1.0, 1.0));
            if (abs(rel.x) > 1.0 || abs(rel.y) > 1.0) continue;
            if (style == 2u && dot(rel, rel) > 1.0) continue;

            float2 tile_uv = rel * 0.5 + 0.5;
            float u_atlas = (float(icon) + clamp(tile_uv.x, 0.004, 0.996)) / 10.0;
            float v_atlas = clamp(tile_uv.y, 0.004, 0.996);
            float2 ag = atlas.SampleLevel(smooth_, float2(u_atlas, v_atlas), 0);
            float glyph = ag.x * prompt_a;
            float3 white_col = (style == 3u) ? float3(0.56, 0.56, 0.58) : float3(0.98, 0.98, 0.99);
            float3 fg = lerp(float3(0.05, 0.05, 0.07), white_col, ag.y);

            float4 bg;
            if (style == 0u) {
                float margin = 7.0 * scale;
                float4 bg_top = over.SampleLevel(smooth_, float2(px.x, cpos.y - half_sz.y - margin) / float2(w, h), 0);
                float4 bg_bot = over.SampleLevel(smooth_, float2(px.x, cpos.y + half_sz.y + margin) / float2(w, h), 0);
                bg = (dot(bg_top.rgb, LUMA) >= dot(bg_bot.rgb, LUMA)) ? bg_top : bg_bot;
                if (bg.a < 0.25) bg = float4(0.0, 0.0, 0.0, 0.0);
            } else {
                // Soft dark drop-shadow halo behind floating PC keycaps so they stay readable against bright sky.
                float2 off_uv = float2(0.0018, 0.018);
                float sh = 0.0;
                sh = max(sh, atlas.SampleLevel(smooth_, float2(u_atlas + off_uv.x, v_atlas + off_uv.y), 0).x);
                sh = max(sh, atlas.SampleLevel(smooth_, float2(u_atlas - off_uv.x, v_atlas + off_uv.y), 0).x);
                sh = max(sh, atlas.SampleLevel(smooth_, float2(u_atlas, v_atlas + off_uv.y * 1.4), 0).x);
                float shadow_a = sh * 0.42 * prompt_a;
                bg = float4(0.02 * shadow_a, 0.02 * shadow_a, 0.03 * shadow_a, shadow_a);
            }

            // Premultiplied alpha blend of PC icon over clean background (`bg` is already premultiplied).
            float3 out_rgb = fg * glyph + bg.rgb * (1.0 - glyph);
            float out_a = saturate(glyph + bg.a * (1.0 - glyph));
            o = float4(out_rgb, out_a);
            break;
        }
    }

    return float4(saturate(o.rgb + s * (1.0 - saturate(o.a))), 1.0);
}
"#;

pub(crate) struct Composer {
    // Graphics composite pass
    root: ID3D12RootSignature,
    pso: ID3D12PipelineState,
    // Compute prompt-detection passes (clear, detect, merge)
    cs_root: ID3D12RootSignature,
    pso_clear: ID3D12PipelineState,
    pso_detect: ID3D12PipelineState,
    pso_merge: ID3D12PipelineState,
    // Per-slot shader-visible descriptor heaps (6 descriptors each: 4 gfx SRVs + 2 compute descriptors)
    views: Vec<ID3D12DescriptorHeap>,
    step: u32,
    atlas: ID3D12Resource,
    atlas_upload: Option<ID3D12Resource>,
    prompts: ID3D12Resource,
}
// SAFETY: D3D12 objects are free-threaded.
unsafe impl Send for Composer {}
// SAFETY: as above.
unsafe impl Sync for Composer {}

fn compile_src(
    source: &str,
    name: PCSTR,
    define: Option<&[u8]>,
    entry: PCSTR,
    target: PCSTR,
) -> windows::core::Result<ID3DBlob> {
    use windows::Win32::Graphics::Direct3D::D3D_SHADER_MACRO;
    let macros = define.map(|d| {
        [
            D3D_SHADER_MACRO {
                Name: PCSTR(d.as_ptr()),
                Definition: s!("1"),
            },
            D3D_SHADER_MACRO::default(),
        ]
    });
    let mut code: Option<ID3DBlob> = None;
    let mut errs: Option<ID3DBlob> = None;
    // SAFETY: static source/names/macros live through the call.
    let res = unsafe {
        D3DCompile(
            source.as_ptr().cast(),
            source.len(),
            name,
            macros.as_ref().map(|m| m.as_ptr()),
            None,
            entry,
            target,
            0,
            0,
            &mut code,
            Some(&mut errs),
        )
    };
    if let Err(e) = res {
        if let Some(blob) = errs {
            // SAFETY: compiler error message blob.
            let msg = unsafe {
                let s = std::slice::from_raw_parts(blob.GetBufferPointer().cast::<u8>(), blob.GetBufferSize());
                String::from_utf8_lossy(s).into_owned()
            };
            tracing::warn!(error = %msg, "compose shader compile failed");
        }
        return Err(e);
    }
    code.ok_or_else(windows::core::Error::empty)
}

fn make_compute_pso(
    device: &ID3D12Device,
    root: &ID3D12RootSignature,
    blob: &ID3DBlob,
) -> windows::core::Result<ID3D12PipelineState> {
    // SAFETY: blob bytes and root signature are valid.
    unsafe {
        let bytes = std::slice::from_raw_parts(blob.GetBufferPointer().cast::<u8>(), blob.GetBufferSize());
        device.CreateComputePipelineState(&D3D12_COMPUTE_PIPELINE_STATE_DESC {
            pRootSignature: std::mem::transmute_copy(root),
            CS: D3D12_SHADER_BYTECODE {
                pShaderBytecode: bytes.as_ptr().cast(),
                BytecodeLength: bytes.len(),
            },
            ..Default::default()
        })
    }
}

impl Composer {
    pub(crate) fn new(host: &Host, format: DXGI_FORMAT) -> windows::core::Result<Self> {
        let (vs, ps) = (
            compile_src(GRAPHICS_SOURCE, s!("compose"), None, s!("vs"), s!("vs_5_1"))?,
            compile_src(GRAPHICS_SOURCE, s!("compose"), None, s!("ps"), s!("ps_5_1"))?,
        );
        let b_clear = compile_src(DETECT_SOURCE, s!("prompts"), Some(b"CLEAR_HITS\0"), s!("clear_hits"), s!("cs_5_1"))?;
        let b_detect = compile_src(DETECT_SOURCE, s!("prompts"), Some(b"DETECT_HITS\0"), s!("detect_hits"), s!("cs_5_1"))?;
        let b_merge = compile_src(DETECT_SOURCE, s!("prompts"), Some(b"MERGE_HITS\0"), s!("merge_hits"), s!("cs_5_1"))?;

        // SAFETY: the blobs' own bytes, alive through the calls.
        unsafe {
            let bytes = |b: &ID3DBlob| {
                std::slice::from_raw_parts(b.GetBufferPointer().cast::<u8>(), b.GetBufferSize())
            };
            let device = &host.device;
            let root: ID3D12RootSignature = device.CreateRootSignature(0, bytes(&vs))?;
            let cs_root: ID3D12RootSignature = device.CreateRootSignature(0, bytes(&b_clear))?;
            let code = |b: &ID3DBlob| D3D12_SHADER_BYTECODE {
                pShaderBytecode: b.GetBufferPointer(),
                BytecodeLength: b.GetBufferSize(),
            };
            let mut desc = D3D12_GRAPHICS_PIPELINE_STATE_DESC {
                pRootSignature: std::mem::transmute_copy(&root),
                VS: code(&vs),
                PS: code(&ps),
                SampleMask: u32::MAX,
                RasterizerState: D3D12_RASTERIZER_DESC {
                    FillMode: D3D12_FILL_MODE_SOLID,
                    CullMode: D3D12_CULL_MODE_NONE,
                    ..Default::default()
                },
                PrimitiveTopologyType: D3D12_PRIMITIVE_TOPOLOGY_TYPE_TRIANGLE,
                NumRenderTargets: 1,
                SampleDesc: DXGI_SAMPLE_DESC {
                    Count: 1,
                    Quality: 0,
                },
                ..Default::default()
            };
            desc.RTVFormats[0] = format;
            desc.BlendState.RenderTarget[0].RenderTargetWriteMask =
                D3D12_COLOR_WRITE_ENABLE_ALL.0 as u8;
            let pso = device.CreateGraphicsPipelineState(&desc)?;
            let pso_clear = make_compute_pso(device, &cs_root, &b_clear)?;
            let pso_detect = make_compute_pso(device, &cs_root, &b_detect)?;
            let pso_merge = make_compute_pso(device, &cs_root, &b_merge)?;

            let default_heap = D3D12_HEAP_PROPERTIES {
                Type: D3D12_HEAP_TYPE_DEFAULT,
                ..Default::default()
            };
            let upload_heap = D3D12_HEAP_PROPERTIES {
                Type: D3D12_HEAP_TYPE_UPLOAD,
                ..Default::default()
            };

            let atlas_desc = D3D12_RESOURCE_DESC {
                Dimension: D3D12_RESOURCE_DIMENSION_TEXTURE2D,
                Width: u64::from(ATLAS_W),
                Height: ATLAS_H,
                DepthOrArraySize: 1,
                MipLevels: 1,
                Format: DXGI_FORMAT_R8G8_UNORM,
                SampleDesc: DXGI_SAMPLE_DESC { Count: 1, Quality: 0 },
                Layout: D3D12_TEXTURE_LAYOUT_UNKNOWN,
                Flags: D3D12_RESOURCE_FLAG_NONE,
                ..Default::default()
            };
            let mut atlas: Option<ID3D12Resource> = None;
            device.CreateCommittedResource(
                &default_heap,
                D3D12_HEAP_FLAG_NONE,
                &atlas_desc,
                D3D12_RESOURCE_STATE_COPY_DEST,
                None,
                &mut atlas,
            )?;
            let atlas = atlas.ok_or_else(windows::core::Error::empty)?;

            let row_bytes = ATLAS_W * ATLAS_BPP;
            let row_pitch = (row_bytes + 255) & !255;
            let upload_bytes = u64::from(row_pitch * ATLAS_H);
            let up_desc = D3D12_RESOURCE_DESC {
                Dimension: D3D12_RESOURCE_DIMENSION_BUFFER,
                Width: upload_bytes,
                Height: 1,
                DepthOrArraySize: 1,
                MipLevels: 1,
                Format: DXGI_FORMAT_UNKNOWN,
                SampleDesc: DXGI_SAMPLE_DESC { Count: 1, Quality: 0 },
                Layout: D3D12_TEXTURE_LAYOUT_ROW_MAJOR,
                Flags: D3D12_RESOURCE_FLAG_NONE,
                ..Default::default()
            };
            let mut atlas_upload: Option<ID3D12Resource> = None;
            device.CreateCommittedResource(
                &upload_heap,
                D3D12_HEAP_FLAG_NONE,
                &up_desc,
                D3D12_RESOURCE_STATE_GENERIC_READ,
                None,
                &mut atlas_upload,
            )?;
            let atlas_upload = atlas_upload.ok_or_else(windows::core::Error::empty)?;
            let mut mapped = std::ptr::null_mut();
            atlas_upload.Map(0, None, Some(&mut mapped))?;
            let dst = mapped.cast::<u8>();
            for y in 0..ATLAS_H as usize {
                std::ptr::copy_nonoverlapping(
                    ATLAS_BYTES.as_ptr().add(y * row_bytes as usize),
                    dst.add(y * row_pitch as usize),
                    row_bytes as usize,
                );
            }
            atlas_upload.Unmap(0, None);

            let buf_desc = D3D12_RESOURCE_DESC {
                Dimension: D3D12_RESOURCE_DIMENSION_BUFFER,
                Width: 2048,
                Height: 1,
                DepthOrArraySize: 1,
                MipLevels: 1,
                Format: DXGI_FORMAT_UNKNOWN,
                SampleDesc: DXGI_SAMPLE_DESC { Count: 1, Quality: 0 },
                Layout: D3D12_TEXTURE_LAYOUT_ROW_MAJOR,
                Flags: D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS,
                ..Default::default()
            };
            let mut prompts: Option<ID3D12Resource> = None;
            device.CreateCommittedResource(
                &default_heap,
                D3D12_HEAP_FLAG_NONE,
                &buf_desc,
                D3D12_RESOURCE_STATE_UNORDERED_ACCESS,
                None,
                &mut prompts,
            )?;
            let prompts = prompts.ok_or_else(windows::core::Error::empty)?;

            Ok(Composer {
                root,
                pso,
                cs_root,
                pso_clear,
                pso_detect,
                pso_merge,
                views: Vec::new(),
                step: device
                    .GetDescriptorHandleIncrementSize(D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV),
                atlas,
                atlas_upload: Some(atlas_upload),
                prompts,
            })
        }
    }

    /// Each presenter command-list slot owns its own 6 descriptors (0..3 graphics SRVs,
    /// 4..5 compute UI SRV + prompts raw UAV). The swap chain may have several
    /// outstanding presents; the slot is selected by the completion fence.
    pub(crate) fn record(
        &mut self,
        host: &Host,
        slot: usize,
        list: &ID3D12GraphicsCommandList,
        planes: [(&ID3D12Resource, DXGI_FORMAT); 2],
        back: &ID3D12Resource,
        (width, height): (u32, u32),
    ) -> windows::core::Result<()> {
        while self.views.len() <= slot {
            let heap = unsafe {
                host.device
                    .CreateDescriptorHeap(&D3D12_DESCRIPTOR_HEAP_DESC {
                        Type: D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV,
                        NumDescriptors: 6,
                        Flags: D3D12_DESCRIPTOR_HEAP_FLAG_SHADER_VISIBLE,
                        NodeMask: 0,
                    })?
            };
            self.views.push(heap);
        }
        let views = &self.views[slot];
        let do_prompts =
            pc_prompts_enabled() && !REAL_PAD_CONNECTED.load(Ordering::Relaxed) && width >= 320 && height >= 240;
        let t = crate::host::transition;

        // SAFETY: an open list; the views are made in slots no frame in flight
        // reads; live textures.
        unsafe {
            // One-time GPU upload of the 1280x128 R8G8 PC prompts atlas on the first frame.
            if let Some(upload) = self.atlas_upload.take() {
                let row_pitch = (ATLAS_W * ATLAS_BPP + 255) & !255;
                let dst_loc = D3D12_TEXTURE_COPY_LOCATION {
                    pResource: std::mem::transmute_copy(&self.atlas),
                    Type: D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX,
                    Anonymous: D3D12_TEXTURE_COPY_LOCATION_0 { SubresourceIndex: 0 },
                };
                let src_loc = D3D12_TEXTURE_COPY_LOCATION {
                    pResource: std::mem::transmute_copy(&upload),
                    Type: D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT,
                    Anonymous: D3D12_TEXTURE_COPY_LOCATION_0 {
                        PlacedFootprint: D3D12_PLACED_SUBRESOURCE_FOOTPRINT {
                            Offset: 0,
                            Footprint: D3D12_SUBRESOURCE_FOOTPRINT {
                                Format: DXGI_FORMAT_R8G8_UNORM,
                                Width: ATLAS_W,
                                Height: ATLAS_H,
                                Depth: 1,
                                RowPitch: row_pitch,
                            },
                        },
                    },
                };
                list.CopyTextureRegion(&dst_loc, 0, 0, 0, &src_loc, None);
                list.ResourceBarrier(&[t(
                    &self.atlas,
                    D3D12_RESOURCE_STATE_COPY_DEST,
                    D3D12_RESOURCE_STATE_PIXEL_SHADER_RESOURCE,
                )]);
                std::mem::forget(upload);
                std::mem::forget(dst_loc);
                std::mem::forget(src_loc);
            }

            let (cpu, gpu) = (
                views.GetCPUDescriptorHandleForHeapStart(),
                views.GetGPUDescriptorHandleForHeapStart(),
            );
            let cpu_at = |i: usize| D3D12_CPU_DESCRIPTOR_HANDLE {
                ptr: cpu.ptr + i * self.step as usize,
            };
            let gpu_at = |i: usize| D3D12_GPU_DESCRIPTOR_HANDLE {
                ptr: gpu.ptr + (i as u64) * u64::from(self.step),
            };

            let tex_srv = |fmt: DXGI_FORMAT| D3D12_SHADER_RESOURCE_VIEW_DESC {
                Format: fmt,
                ViewDimension: D3D12_SRV_DIMENSION_TEXTURE2D,
                Shader4ComponentMapping: D3D12_DEFAULT_SHADER_4_COMPONENT_MAPPING,
                Anonymous: D3D12_SHADER_RESOURCE_VIEW_DESC_0 {
                    Texture2D: D3D12_TEX2D_SRV {
                        MipLevels: 1,
                        ..Default::default()
                    },
                },
            };

            // Graphics descriptors 0..3: scene, over, atlas, prompts raw SRV
            host.device.CreateShaderResourceView(Some(planes[0].0), Some(&tex_srv(planes[0].1)), cpu_at(0));
            host.device.CreateShaderResourceView(Some(planes[1].0), Some(&tex_srv(planes[1].1)), cpu_at(1));
            host.device.CreateShaderResourceView(Some(&self.atlas), Some(&tex_srv(DXGI_FORMAT_R8G8_UNORM)), cpu_at(2));
            let prompts_srv = D3D12_SHADER_RESOURCE_VIEW_DESC {
                Format: DXGI_FORMAT_R32_TYPELESS,
                ViewDimension: D3D12_SRV_DIMENSION_BUFFER,
                Shader4ComponentMapping: D3D12_DEFAULT_SHADER_4_COMPONENT_MAPPING,
                Anonymous: D3D12_SHADER_RESOURCE_VIEW_DESC_0 {
                    Buffer: D3D12_BUFFER_SRV {
                        FirstElement: 0,
                        NumElements: 512,
                        StructureByteStride: 0,
                        Flags: D3D12_BUFFER_SRV_FLAG_RAW,
                    },
                },
            };
            host.device.CreateShaderResourceView(Some(&self.prompts), Some(&prompts_srv), cpu_at(3));

            // Compute descriptors 4..5: over SRV, prompts raw UAV
            host.device.CreateShaderResourceView(Some(planes[1].0), Some(&tex_srv(planes[1].1)), cpu_at(4));
            let prompts_uav = D3D12_UNORDERED_ACCESS_VIEW_DESC {
                Format: DXGI_FORMAT_R32_TYPELESS,
                ViewDimension: D3D12_UAV_DIMENSION_BUFFER,
                Anonymous: D3D12_UNORDERED_ACCESS_VIEW_DESC_0 {
                    Buffer: D3D12_BUFFER_UAV {
                        FirstElement: 0,
                        NumElements: 512,
                        StructureByteStride: 0,
                        CounterOffsetInBytes: 0,
                        Flags: D3D12_BUFFER_UAV_FLAG_RAW,
                    },
                },
            };
            host.device.CreateUnorderedAccessView(
                Some(&self.prompts),
                None::<&ID3D12Resource>,
                Some(&prompts_uav),
                cpu_at(5),
            );

            list.SetDescriptorHeaps(&[Some(views.clone())]);

            if do_prompts {
                let uav_bar = [D3D12_RESOURCE_BARRIER {
                    Type: D3D12_RESOURCE_BARRIER_TYPE_UAV,
                    Flags: D3D12_RESOURCE_BARRIER_FLAG_NONE,
                    Anonymous: D3D12_RESOURCE_BARRIER_0 {
                        UAV: std::mem::ManuallyDrop::new(D3D12_RESOURCE_UAV_BARRIER {
                            pResource: std::mem::transmute_copy(&self.prompts),
                        }),
                    },
                }];
                list.ResourceBarrier(&[t(
                    planes[1].0,
                    D3D12_RESOURCE_STATE_PIXEL_SHADER_RESOURCE,
                    D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE,
                )]);
                let cs_consts: [u32; 2] = [width, height];
                list.SetComputeRootSignature(&self.cs_root);
                list.SetComputeRoot32BitConstants(0, 2, cs_consts.as_ptr().cast(), 0);
                list.SetComputeRootDescriptorTable(1, gpu_at(4));

                list.SetPipelineState(&self.pso_clear);
                list.Dispatch(1, 1, 1);
                list.ResourceBarrier(&uav_bar);

                list.SetPipelineState(&self.pso_detect);
                list.Dispatch(width.div_ceil(16), height.div_ceil(16), 1);
                list.ResourceBarrier(&uav_bar);

                list.SetPipelineState(&self.pso_merge);
                list.Dispatch(1, 1, 1);

                list.ResourceBarrier(&[
                    t(
                        planes[1].0,
                        D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE,
                        D3D12_RESOURCE_STATE_PIXEL_SHADER_RESOURCE,
                    ),
                    t(
                        &self.prompts,
                        D3D12_RESOURCE_STATE_UNORDERED_ACCESS,
                        D3D12_RESOURCE_STATE_PIXEL_SHADER_RESOURCE,
                    ),
                ]);
            }

            let gfx_consts: [u32; 4] = [
                width,
                height,
                sharpen_amount().to_bits(),
                u32::from(do_prompts),
            ];
            let rtv = host.rtv(Some(back), None);
            list.OMSetRenderTargets(1, Some(&rtv), false, None);
            list.SetGraphicsRootSignature(&self.root);
            list.SetPipelineState(&self.pso);
            list.SetGraphicsRoot32BitConstants(0, 4, gfx_consts.as_ptr().cast(), 0);
            list.SetGraphicsRootDescriptorTable(1, gpu_at(0));
            list.IASetPrimitiveTopology(
                windows::Win32::Graphics::Direct3D::D3D_PRIMITIVE_TOPOLOGY_TRIANGLELIST,
            );
            list.RSSetViewports(&[D3D12_VIEWPORT {
                Width: width as f32,
                Height: height as f32,
                MaxDepth: 1.0,
                ..Default::default()
            }]);
            list.RSSetScissorRects(&[windows::Win32::Foundation::RECT {
                left: 0,
                top: 0,
                right: width as i32,
                bottom: height as i32,
            }]);
            list.DrawInstanced(3, 1, 0, 0);

            if do_prompts {
                list.ResourceBarrier(&[t(
                    &self.prompts,
                    D3D12_RESOURCE_STATE_PIXEL_SHADER_RESOURCE,
                    D3D12_RESOURCE_STATE_UNORDERED_ACCESS,
                )]);
            }
        }
        Ok(())
    }
}

/// Select a legal float-sampled view of a display plane, preserving a typed
/// resource's format. Typeless resources use the corresponding display type.
pub(crate) fn view(d: &D3D12_RESOURCE_DESC) -> Option<DXGI_FORMAT> {
    if d.Dimension != D3D12_RESOURCE_DIMENSION_TEXTURE2D
        || d.DepthOrArraySize != 1
        || d.SampleDesc.Count != 1
        || d.Flags.contains(D3D12_RESOURCE_FLAG_DENY_SHADER_RESOURCE)
    {
        return None;
    }
    match d.Format.0 {
        9 => Some(DXGI_FORMAT_R16G16B16A16_FLOAT),
        23 => Some(DXGI_FORMAT_R10G10B10A2_UNORM),
        27 => Some(DXGI_FORMAT_R8G8B8A8_UNORM),
        90 => Some(DXGI_FORMAT_B8G8R8A8_UNORM),
        10 | 11 | 13 | 24 | 26 | 28 | 29 | 31 | 67 | 87 | 91 => Some(d.Format),
        _ => None,
    }
}

#[cfg(all(test, feature = "diagnostics"))]
mod tests {
    use super::*;

    #[test]
    fn compose_and_pc_prompt_shaders_compile_cleanly() {
        for (src, name, def, entry, target) in [
            (GRAPHICS_SOURCE, s!("compose"), None, s!("vs"), s!("vs_5_1")),
            (GRAPHICS_SOURCE, s!("compose"), None, s!("ps"), s!("ps_5_1")),
            (DETECT_SOURCE, s!("prompts"), Some(&b"CLEAR_HITS\0"[..]), s!("clear_hits"), s!("cs_5_1")),
            (DETECT_SOURCE, s!("prompts"), Some(&b"DETECT_HITS\0"[..]), s!("detect_hits"), s!("cs_5_1")),
            (DETECT_SOURCE, s!("prompts"), Some(&b"MERGE_HITS\0"[..]), s!("merge_hits"), s!("cs_5_1")),
        ] {
            let blob = compile_src(src, name, def, entry, target).expect("HLSL shader should compile");
            // SAFETY: valid compiled D3DBlob.
            assert!(unsafe { blob.GetBufferSize() } > 0);
        }
    }

    #[test]
    fn composes_scaled_premultiplied_plane_on_gpu() {
        let Some(host) = crate::host::host() else {
            eprintln!("skipping display-plane readback: no D3D12 device");
            return;
        };
        let format = DXGI_FORMAT_R8G8B8A8_UNORM;
        let mut compositor = Composer::new(host, format).unwrap();
        let list = host.new_list().unwrap();
        let texture = |width: u64| -> ID3D12Resource {
            let mut resource = None;
            let desc = D3D12_RESOURCE_DESC {
                Dimension: D3D12_RESOURCE_DIMENSION_TEXTURE2D,
                Width: width,
                Height: 1,
                DepthOrArraySize: 1,
                MipLevels: 1,
                Format: format,
                SampleDesc: DXGI_SAMPLE_DESC {
                    Count: 1,
                    Quality: 0,
                },
                Flags: D3D12_RESOURCE_FLAG_ALLOW_RENDER_TARGET,
                ..Default::default()
            };
            unsafe {
                host.device
                    .CreateCommittedResource(
                        &D3D12_HEAP_PROPERTIES {
                            Type: D3D12_HEAP_TYPE_DEFAULT,
                            ..Default::default()
                        },
                        D3D12_HEAP_FLAG_NONE,
                        &desc,
                        D3D12_RESOURCE_STATE_RENDER_TARGET,
                        None,
                        &mut resource,
                    )
                    .unwrap()
            };
            resource.unwrap()
        };
        let base = texture(1);
        let overlay = texture(3);
        let output = texture(3);
        let target = D3D12_RESOURCE_STATE_RENDER_TARGET;
        let read = D3D12_RESOURCE_STATE_PIXEL_SHADER_RESOURCE;
        let cmds = &list.list;
        unsafe {
            cmds.ClearRenderTargetView(host.rtv(Some(&base), None), &[0.5, 0.0, 0.0, 1.0], None);
            let overlay_rtv = host.rtv(Some(&overlay), None);
            let colors = [
                [0.0, 0.0, 0.0, 0.0],
                [0.0, 0.5, 0.0, 0.5],
                [0.0, 0.0, 1.0, 1.0],
            ];
            for (i, color) in colors.iter().enumerate() {
                let rect = windows::Win32::Foundation::RECT {
                    left: i as i32,
                    right: i as i32 + 1,
                    top: 0,
                    bottom: 1,
                };
                cmds.ClearRenderTargetView(overlay_rtv, color, Some(&[rect]));
            }
            cmds.ResourceBarrier(&[
                crate::host::transition(&base, target, read),
                crate::host::transition(&overlay, target, read),
            ]);
            compositor
                .record(
                    host,
                    0,
                    cmds,
                    [(&base, format), (&overlay, format)],
                    &output,
                    (3, 1),
                )
                .unwrap();
            cmds.ResourceBarrier(&[crate::host::transition(
                &output,
                target,
                D3D12_RESOURCE_STATE_COPY_SOURCE,
            )]);
            let (buffer, footprint) =
                crate::capture::read_back(&host.device, cmds, &output).unwrap();
            list.close();
            host.execute(&[&list]);
            host.device.GetDeviceRemovedReason().unwrap();
            let mut mapped = std::ptr::null_mut();
            buffer.Map(0, None, Some(&mut mapped)).unwrap();
            let start = mapped.cast::<u8>().add(footprint.Offset as usize);
            let pixels: [u8; 12] = std::slice::from_raw_parts(start, 12).try_into().unwrap();
            buffer.Unmap(0, None);
            for (actual, expected) in pixels
                .iter()
                .zip([128, 0, 0, 255, 64, 128, 0, 255, 0, 0, 255, 255])
            {
                assert!(
                    actual.abs_diff(expected) <= 1,
                    "unexpected composite {pixels:?}"
                );
            }
            eprintln!("D3D12 display-plane readback RGBA: {pixels:?}");
        }
    }
}
