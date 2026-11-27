//! Ported from fxceless/victory-gta6 PR #1189 (Series S interface plane), `VICTORY_COMPOSE=1`.
//! A present's second plane laid over its first, with optional lightweight
//! Contrast Adaptive Sharpening (`VICTORY_SHARPEN`) on the game plane and
//! real-time Xbox -> PC keycap/mouse prompt icon replacement (`VICTORY_PC_PROMPTS=1`)
//! on the pure UI plane when playing with keyboard & mouse.

use std::sync::Mutex;
use std::sync::atomic::{AtomicBool, Ordering};

use windows::Win32::Graphics::Direct3D::Fxc::D3DCompile;
use windows::Win32::Graphics::Direct3D::ID3DBlob;
use windows::Win32::Graphics::Direct3D12::*;
use windows::Win32::Graphics::Dxgi::Common::*;

/// Frames of descriptors in the ring (five each): presents in flight
/// must not find theirs rewritten.
const SLOTS: u32 = 16;
const SLOTS_PER_FRAME: u32 = 5;

/// Embedded 1280x128 R8G8_UNORM alpha+ink atlas (10 crisp 128x128 8x-supersampled GTA VI-style PC prompt icons):
/// R = alpha coverage (0 outside silhouette -> guaranteed zero black box),
/// G = ink luminance (0 = dark outline/letter, 1 = pure white keycap/active mouse button).
/// 0: RMB (LT), 1: LMB (RT), 2: TAB (LB), 3: G (RB), 4: MAJ (A),
/// 5: R (B), 6: SPACE (X), 7: F (Y), 8: E (Interact/Talk A), 9: C (RS).
const ATLAS_W: u32 = 1280;
const ATLAS_H: u32 = 128;
const ATLAS_BPP: u32 = 2;
const ATLAS_BYTES: &[u8] = include_bytes!("pc_prompts_atlas.bin");
const _: () = assert!(ATLAS_BYTES.len() == (ATLAS_W * ATLAS_H * ATLAS_BPP) as usize);

/// Updated by `victory_hle::modules::gameinput`: when a real XInput gamepad is
/// connected, keep the original Xbox button icons automatically.
static REAL_PAD_CONNECTED: AtomicBool = AtomicBool::new(false);

pub fn set_real_pad_connected(connected: bool) {
    REAL_PAD_CONNECTED.store(connected, Ordering::Relaxed);
}

const SOURCE: &str = r#"
#define RS "RootConstants(num32BitConstants=6, b0), DescriptorTable(SRV(t0, numDescriptors=3), UAV(u0, numDescriptors=2)), StaticSampler(s0, filter=FILTER_MIN_MAG_MIP_LINEAR, addressU=TEXTURE_ADDRESS_CLAMP, addressV=TEXTURE_ADDRESS_CLAMP)"

cbuffer Params : register(b0) {
    uint w;
    uint h;
    uint premultiplied;
    float sharpen;
    uint pc_prompts;
    float vibrance;
};

Texture2D<float4> game : register(t0);
Texture2D<float4> ui : register(t1);
Texture2D<float2> atlas : register(t2);
RWTexture2D<float4> outp : register(u0);

// Prompt detection state (raw u32 buffer):
// dword 0: raw candidate count (0..64)
// dwords 1..1 + 64*4: raw candidates (cx, cy, packed_kind_flags, score_u32)
//   packed_kind_flags: bits 0..7 = icon_id (0..9), bits 8..15 = style (0=box popup, 1=floating circle, 2=ring inner)
// dword 260: merged hit count (0..8)
// dwords 261..261 + 8*6: merged hits (cx, cy, hw, hh, icon_id, style)
RWByteAddressBuffer prompts : register(u1);

SamplerState linear_clamp : register(s0);

static const float3 LUMA = float3(0.299, 0.587, 0.114);
static const float TWO_PI = 6.28318530718;

float4 sample_ui_px(float2 px) {
    float2 uv = (px + 0.5) / float2(w, h);
    return ui.SampleLevel(linear_clamp, uv, 0);
}

bool is_popup_bg(float4 c) {
    float l = dot(c.rgb, LUMA);
    float hi = max(c.r, max(c.g, c.b));
    float lo = min(c.r, min(c.g, c.b));
    return c.a > 0.55 && l < 0.24 && (hi - lo) < 0.08;
}

bool is_white_glyph(float4 c) {
    float l = dot(c.rgb, LUMA);
    float hi = max(c.r, max(c.g, c.b));
    float lo = min(c.r, min(c.g, c.b));
    return c.a > 0.78 && l > 0.70 && (hi - lo) < 0.22;
}

// Also matches GTA VI's disabled/dimmed grey circular prompt buttons (e.g. inactive "PARLER (A)").
bool is_prompt_disc(float4 c) {
    float l = dot(c.rgb, LUMA);
    float hi = max(c.r, max(c.g, c.b));
    float lo = min(c.r, min(c.g, c.b));
    return c.a > 0.72 && l > 0.36 && (hi - lo) < 0.16;
}

bool is_dark_ink(float4 c) {
    float l = dot(c.rgb, LUMA);
    return c.a > 0.50 && l < 0.32;
}

// 4: A (green), 5: B (red), 6: X (blue), 7: Y (yellow), -1: none
int classify_abxy_color(float4 c) {
    if (c.a < 0.75) return -1;
    if (c.g > 0.42 && (c.g - c.r) > 0.15 && (c.g - c.b) > 0.15) return 4;
    if (c.r > 0.52 && (c.r - c.g) > 0.20 && (c.r - c.b) > 0.20) return 5;
    if (c.b > 0.48 && (c.b - c.r) > 0.18 && c.g > 0.18) return 6;
    if (c.r > 0.62 && c.g > 0.50 && c.b < 0.30 && (c.r - c.b) > 0.38) return 7;
    return -1;
}

// Classifies the black letter inside GTA VI's monochrome white circular button
// using translation-invariant ink-bbox recentering + 5x5 Zero-Mean Normalized
// Cross-Correlation (ZNCC), verified against real 1080p GTA VI screenshots:
// A -> E (tile 8), X -> SPACE (tile 6), R (right stick) -> Mouse (tile 0),
// B -> R (tile 5), Y -> F (tile 7).
int classify_white_circle_letter(float2 p, float scale) {
    // 1) Find dark-ink bounding box strictly inside disk radius <= 6.2*scale
    //    so outer ring outline or adjacent words never contaminate the letter bbox.
    float minx = 100.0, maxx = -100.0, miny = 100.0, maxy = -100.0;
    uint found = 0u;
    for (int j = -6; j <= 6; j++) {
        for (int i = -6; i <= 6; i++) {
            if (i * i + j * j > 38) continue;
            float4 s = sample_ui_px(p + float2(float(i), float(j)) * scale);
            float l = dot(s.rgb, LUMA);
            if (s.a > 0.50 && l < 0.32) {
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

    // 2) Sample 5x5 ink darkness centered on the exact glyph bbox.
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

    // 3) 5x5 bitmask templates (bit (j*5+i) = 1 where dark ink is expected).
    // A(8): 00100 / 01010 / 01010 / 11111 / 10001
    // X(6): 10001 / 01010 / 00100 / 01010 / 10001
    // R(0): 11110 / 10010 / 11100 / 10100 / 10010
    // B(5): 11110 / 10010 / 11110 / 10010 / 11110
    // Y(7): 10001 / 01010 / 00100 / 00100 / 00100
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

void push_candidate(float2 p, uint icon, uint style, float score) {
    uint slot;
    prompts.InterlockedAdd(0, 1, slot);
    if (slot < 64u) {
        uint base = (1u + slot * 4u) * 4u;
        uint packed = (icon & 0xFFu) | ((style & 0xFFu) << 8);
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
// Runs on a 2px grid (w/2 x h/2): detects Xbox button prompts both inside dark HUD
// pop-up boxes (top-left) and floating interaction prompts (bottom-right & bottom-center).
[RootSignature(RS)]
[numthreads(8, 8, 1)]
void detect_hits(uint3 tid : SV_DispatchThreadID) {
    float scale = float(h) / 1080.0;
    float2 p = float2(tid.xy * 2u + 1u);
    if (p.x < 26.0 * scale || p.x >= float(w) - 26.0 * scale ||
        p.y < 26.0 * scale || p.y >= float(h) - 26.0 * scale) {
        return;
    }

    // Global full-screen UI guard: during loading screens (artwork) or full-screen menus,
    // the UI plane is opaque across the middle of the screen. In gameplay, the UI plane is
    // transparent over the 3D scene -> bail out immediately if >= 3 of 5 center probes are opaque.
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
    // Bottom-right ("PARLER (A)", "MENACER (X)", "YOLANDA / EMPLOYE ((LT))")
    // and bottom-center ("Maintenez [LT] pour viser.", "Appuyez sur (X) pour menacer.", "Utilisez (R)...").
    bool in_bottom_right = (p.x > float(w) * 0.72 && p.y > float(h) * 0.55);
    bool in_bottom_center = (p.x > float(w) * 0.25 && p.x < float(w) * 0.78 && p.y > float(h) * 0.85);
    if (in_bottom_right || in_bottom_center) {
        // A1) Hold-ring prompt (bottom-right): crisp white ring at radius ~18px, transparent outside at ~25px.
        if (in_bottom_right) {
            bool outer_clear =
                sample_ui_px(p + float2(0.0, -25.0 * scale)).a < 0.18 &&
                sample_ui_px(p + float2(0.0,  25.0 * scale)).a < 0.18 &&
                sample_ui_px(p + float2(25.0 * scale, 0.0)).a < 0.18;
            if (outer_clear) {
                uint ring18 = 0u;
                uint ring25_clear = 0u;
                for (uint k = 0u; k < 16u; k++) {
                    float ang = float(k) * (TWO_PI / 16.0);
                    float2 dir = float2(cos(ang), sin(ang));
                    if (is_white_glyph(sample_ui_px(p + dir * (18.0 * scale)))) ring18++;
                    if (sample_ui_px(p + dir * (25.0 * scale)).a < 0.18) ring25_clear++;
                }
                if (ring18 >= 13u && ring25_clear >= 10u) {
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
                    if (fill >= 0.42 && inner_d >= 2u) {
                        uint icon;
                        if (abs(bl - br) >= 0.9 && (tl + tr) >= 2.5) {
                            icon = (br > bl) ? 1u : 0u; // LT -> RMB (0), RT -> LMB (1)
                        } else {
                            icon = uint(classify_white_circle_letter(p, scale));
                        }
                        push_candidate(p, icon, 2u, fill);
                        return;
                    }
                }
            }
        }

        // A2) Floating LT/RT trigger in bottom-center subtitle prompts (e.g. "Maintenez [LT] pour viser.")
        if (in_bottom_center && center.a > 0.65) {
            float pad_f = 18.0 * scale;
            bool clear_top_bot = sample_ui_px(p + float2(0.0, -pad_f)).a < 0.15
                              && sample_ui_px(p + float2(0.0,  pad_f)).a < 0.15;
            if (clear_top_bot) {
                float half_f = 11.0 * scale;
                float total = 0.0, top_bar = 0.0, bot_bar = 0.0;
                uint inner_d = 0u;
                float tl = 0.0, tr = 0.0, bl = 0.0, br = 0.0;
                for (int j = 0; j < 8; j++) {
                    for (int i = 0; i < 8; i++) {
                        float2 off = float2(-half_f + float(i) * (2.0 * half_f / 7.0),
                                            -half_f + float(j) * (2.0 * half_f / 7.0));
                        float4 s = sample_ui_px(p + off);
                        float v = is_white_glyph(s) ? 1.0 : 0.0;
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
                        push_candidate(p, 0u, 1u, fill);
                        return;
                    }
                    if (tl >= 0.45 && tr >= 0.45 && br >= 0.45 && bl <= 0.35 && (br - bl) >= 0.25) {
                        push_candidate(p, 1u, 1u, fill);
                        return;
                    }
                }
            }
        }

        // A3) Monochrome white or dimmed-grey circular button (PARLER (A), MENACER (X), (B), (Y), (R)):
        // Neutral bright/grey perimeter at radius ~10px, transparent outside at ~17px, dark letter inside.
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
                for (uint k = 0u; k < 12u; k++) {
                    float ang = float(k) * (TWO_PI / 12.0);
                    float2 dir = float2(cos(ang), sin(ang));
                    float4 rs = sample_ui_px(p + dir * (10.0 * scale));
                    if (is_prompt_disc(rs)) {
                        ring10++;
                        ring_luma += dot(rs.rgb, LUMA);
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
                        uint style = (ring_luma / float(ring10) < 0.65) ? 3u : 1u; // 3 = dimmed inactive button
                        push_candidate(p, uint(icon), style, float(ring10) / 12.0);
                        return;
                    }
                }
            }
        }
    }

    // ---- MODE B: Dark pop-up box prompts (top-left, e.g. "Maintenez [LT] pour interagir.") ----
    if (p.x > float(w) * 0.52 || p.y > float(h) * 0.42) return;
    if (center.a < 0.65) return;

    float pad = 17.0 * scale;
    float4 top_bg = sample_ui_px(p + float2(0.0, -pad));
    float4 bot_bg = sample_ui_px(p + float2(0.0,  pad));
    float4 l_bg   = sample_ui_px(p + float2(-pad, 0.0));
    float4 r_bg   = sample_ui_px(p + float2( pad, 0.0));
    if (!is_popup_bg(top_bg) || !is_popup_bg(bot_bg)) return;
    if (!is_popup_bg(l_bg) || !is_popup_bg(r_bg)) return;

    // Require uniform charcoal pop-up box color and transparent UI outside the box vertically.
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
        for (int j = 0; j < 6; j++) {
            for (int i = 0; i < 6; i++) {
                float2 off = float2(-half_box + float(i) * (2.0 * half_box / 5.0),
                                    -half_box + float(j) * (2.0 * half_box / 5.0));
                if (classify_abxy_color(sample_ui_px(p + off)) == col_icon) {
                    match_cnt += 1.0;
                }
            }
        }
        float ratio = match_cnt / 36.0;
        if (ratio >= 0.42) {
            push_candidate(p, uint(col_icon), 0u, ratio);
        }
        return;
    }

    // B2) Solid white trigger/bumper silhouettes (LT, RT, LB, RB) with dark inner letters.
    float half_w = 11.5 * scale;
    float half_h = 11.5 * scale;
    float total = 0.0;
    uint inner_dark = 0u;
    float top_bar = 0.0;
    float bot_bar = 0.0;
    float tl = 0.0, tr = 0.0, bl = 0.0, br = 0.0;

    for (int j = 0; j < 8; j++) {
        for (int i = 0; i < 8; i++) {
            float2 off = float2(-half_w + float(i) * (2.0 * half_w / 7.0),
                                -half_h + float(j) * (2.0 * half_h / 7.0));
            float4 s = sample_ui_px(p + off);
            float v = is_white_glyph(s) ? 1.0 : 0.0;
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
    // LT (beveled bottom-right corner) -> RMB (tile 0)
    if (tl >= 0.45 && tr >= 0.45 && bl >= 0.45 && br <= 0.35 && (bl - br) >= 0.25) {
        icon = 0;
    }
    // RT (beveled bottom-left corner) -> LMB (tile 1)
    else if (tl >= 0.45 && tr >= 0.45 && br >= 0.45 && bl <= 0.35 && (br - bl) >= 0.25) {
        icon = 1;
    }
    // LB (beveled top-left corner) -> TAB (tile 2)
    else if (bl >= 0.45 && br >= 0.45 && tr >= 0.45 && tl <= 0.35 && (tr - tl) >= 0.22) {
        icon = 2;
    }
    // RB (beveled top-right corner) -> G (tile 3)
    else if (bl >= 0.45 && br >= 0.45 && tl >= 0.45 && tr <= 0.35 && (tl - tr) >= 0.22) {
        icon = 3;
    }

    if (icon >= 0) {
        push_candidate(p, uint(icon), 0u, fill);
    }
}
#endif

#ifdef MERGE_HITS
[RootSignature(RS)]
[numthreads(1, 1, 1)]
void merge_hits() {
    float scale = float(h) / 1080.0;

    // Clear persistent history immediately on opaque full-screen UI (loading screens / menus).
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

    // Load previous frame's persistent hits (dwords 320..385) for anti-flicker hysteresis.
    uint prev_n = min(prompts.Load(320 * 4), 8u);
    uint4 prev_box[8];
    uint4 prev_meta[8]; // (icon, style, ttl, 0)
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
    uint4 out_meta[8];

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

        // Match against previous frame's hits within 18px to stabilize position & prevent icon flips.
        float match_r = 18.0 * scale;
        for (uint p = 0u; p < prev_n; p++) {
            if (prev_matched[p]) continue;
            float2 pd = float2(cx - float(prev_box[p].x), cy - float(prev_box[p].y));
            if (dot(pd, pd) <= match_r * match_r) {
                prev_matched[p] = true;
                cx = lerp(float(prev_box[p].x), cx, 0.50);
                cy = lerp(float(prev_box[p].y), cy, 0.50);
                if (prev_meta[p].x != icon && best_w < 880u && prev_meta[p].z >= 6u) {
                    icon = prev_meta[p].x;
                }
                break;
            }
        }

        float rad = (style == 2u) ? 14.5 : 15.0;
        uint hw = uint(rad * scale + 0.5);
        uint hh = uint(rad * scale + 0.5);
        out_box[out_n] = uint4(uint(cx + 0.5), uint(cy + 0.5), hw, hh);
        out_meta[out_n] = uint4(icon, style, 10u, 0u);
        out_n++;
    }

    // Carry over unmatched previous hits for up to 10 frames as long as UI pixels
    // are still visible at that exact prompt location (eliminates Xbox <-> PC flicker!).
    for (uint p = 0u; p < prev_n && out_n < 8u; p++) {
        if (prev_matched[p] || prev_meta[p].z <= 1u) continue;
        float2 pc = float2(prev_box[p].xy);
        uint pstyle = prev_meta[p].y;
        float a_center = sample_ui_px(pc).a;
        float a_ring = sample_ui_px(pc + float2((pstyle == 2u ? 18.0 : 9.0) * scale, 0.0)).a;
        if (max(a_center, a_ring) > 0.30) {
            out_box[out_n] = prev_box[p];
            out_meta[out_n] = uint4(prev_meta[p].x, pstyle, prev_meta[p].z - 1u, 0u);
            out_n++;
        }
    }

    // Store active hits for COMPOSE (260..) and persistent state for next frame (320..).
    prompts.Store(260 * 4, out_n);
    prompts.Store(320 * 4, out_n);
    for (uint k = 0u; k < out_n; k++) {
        uint dst = (261u + k * 6u) * 4u;
        prompts.Store4(dst, out_box[k]);
        prompts.Store2(dst + 16u, out_meta[k].xy);

        uint pdst = (321u + k * 8u) * 4u;
        prompts.Store4(pdst, out_box[k]);
        prompts.Store4(pdst + 16u, out_meta[k]);
    }
}
#endif

#ifdef COMPOSE
// Lightweight 5-tap Contrast Adaptive Sharpening (CAS) on the game plane only.
float3 sharpen_game(int2 p) {
    float3 c = game.Load(int3(p, 0)).rgb;
    if (sharpen <= 0.001) return c;
    int2 lim = int2(int(w) - 1, int(h) - 1);
    float3 n = game.Load(int3(clamp(p + int2( 0, -1), int2(0, 0), lim), 0)).rgb;
    float3 s = game.Load(int3(clamp(p + int2( 0,  1), int2(0, 0), lim), 0)).rgb;
    float3 e = game.Load(int3(clamp(p + int2( 1,  0), int2(0, 0), lim), 0)).rgb;
    float3 b = game.Load(int3(clamp(p + int2(-1,  0), int2(0, 0), lim), 0)).rgb;

    float3 mn = min(c, min(min(n, s), min(e, b)));
    float3 mx = max(c, max(max(n, s), max(e, b)));
    float3 amp = saturate(min(mn, 1.0 - mx) / max(mx, 1e-4));
    float3 weight = sqrt(amp) * (-0.16 * sharpen);
    float3 res = (c + (n + s + e + b) * weight) / (1.0 + 4.0 * weight);
    return saturate(res);
}

// Vice City vitality color-grading pass (game plane only, never touches UI/HUD/prompts):
// 1) Gentle shadow lift for indoor readability without washing out pure blacks.
// 2) Smart vibrance that enriches muted/mid-saturated colors while protecting skin tones
//    and already-vivid highlights from clipping.
// 3) Subtle filmic S-curve contrast for crisp depth and lively light response.
float3 grade_game(float3 c) {
    if (vibrance <= 0.001) return c;
    float luma = dot(c, LUMA);
    float shadow_mask = smoothstep(0.015, 0.22, luma) * (1.0 - smoothstep(0.32, 0.78, luma));
    c += c * (0.14 * vibrance * shadow_mask) + float3(0.012, 0.011, 0.010) * (vibrance * shadow_mask);

    float hi = max(c.r, max(c.g, c.b));
    float lo = min(c.r, min(c.g, c.b));
    float sat = (hi > 1e-4) ? ((hi - lo) / hi) : 0.0;
    float l2 = dot(c, LUMA);
    float luma_guard = smoothstep(0.03, 0.14, l2) * (1.0 - smoothstep(0.84, 0.99, l2));
    float skin = saturate(1.0 - abs(c.r - c.g * 1.28) * 3.0) * smoothstep(0.05, 0.20, c.r - c.b);
    float vib_boost = vibrance * (1.0 - sat * 0.72) * luma_guard * (1.0 - 0.30 * skin);
    c = lerp(float3(l2, l2, l2), c, 1.0 + vib_boost * 1.15);

    float3 s_curve = c * c * (3.0 - 2.0 * c);
    c = lerp(c, s_curve, 0.14 * vibrance);
    return saturate(c);
}

[RootSignature(RS)]
[numthreads(8, 8, 1)]
void main(uint3 id : SV_DispatchThreadID) {
    if (id.x >= w || id.y >= h) return;
    float2 uv = (float2(id.xy) + 0.5) / float2(w, h);
    float3 g = grade_game(sharpen_game(int2(id.xy)));
    float4 u = ui.SampleLevel(linear_clamp, uv, 0);

    if (pc_prompts != 0u) {
        float scale = float(h) / 1080.0;
        uint n = min(prompts.Load(260 * 4), 8u);
        for (uint k = 0u; k < n; k++) {
            uint src = (261u + k * 6u) * 4u;
            uint4 box = prompts.Load4(src);
            uint2 meta = prompts.Load2(src + 16u);
            float2 cpos = float2(box.xy);
            float2 half_sz = float2(box.zw);
            uint icon = meta.x;
            uint style = meta.y;

            float2 rel = (float2(id.xy) - cpos) / max(half_sz, float2(1.0, 1.0));
            if (abs(rel.x) > 1.0 || abs(rel.y) > 1.0) continue;
            // For ring-hold prompt (style 2), keep GTA VI's outer white hold ring (r=18px) intact
            // while fully clearing the old [LT] inside r<=14.5px!
            if (style == 2u && dot(rel, rel) > 1.0) continue;

            float2 tile_uv = rel * 0.5 + 0.5;
            float u_atlas = (float(icon) + clamp(tile_uv.x, 0.004, 0.996)) / 10.0;
            float v_atlas = clamp(tile_uv.y, 0.004, 0.996);
            float2 ag = atlas.SampleLevel(linear_clamp, float2(u_atlas, v_atlas), 0);
            float prompt_a = saturate(max(u.a, sample_ui_px(cpos).a) / 0.85);
            prompt_a = max(prompt_a, 0.25);
            float glyph = ag.x * prompt_a;
            float3 white_col = (style == 3u) ? float3(0.56, 0.56, 0.58) : float3(0.98, 0.98, 0.99);
            float3 fg = lerp(float3(0.05, 0.05, 0.07), white_col, ag.y);

            float4 bg;
            if (style == 0u) {
                // Sample clean pop-up box background well outside the 2px icon shadow (+-7px past box edge)
                // so there is NEVER a black square behind the PC icon!
                float margin = 7.0 * scale;
                float4 bg_top = sample_ui_px(float2(float(id.x), cpos.y - half_sz.y - margin));
                float4 bg_bot = sample_ui_px(float2(float(id.x), cpos.y + half_sz.y + margin));
                // Prefer the lighter/less opaque sample to avoid dark borders/drop-shadows.
                bg = (dot(bg_top.rgb, LUMA) >= dot(bg_bot.rgb, LUMA)) ? bg_top : bg_bot;
                if (bg.a < 0.25) bg = float4(0.0, 0.0, 0.0, 0.0);
            } else {
                // Soft outer drop-shadow halo behind floating PC keycaps so they stay readable over bright sky.
                float2 off_uv = float2(0.0018, 0.018);
                float sh = 0.0;
                sh = max(sh, atlas.SampleLevel(linear_clamp, float2(u_atlas + off_uv.x, v_atlas + off_uv.y), 0).x);
                sh = max(sh, atlas.SampleLevel(linear_clamp, float2(u_atlas - off_uv.x, v_atlas + off_uv.y), 0).x);
                sh = max(sh, atlas.SampleLevel(linear_clamp, float2(u_atlas, v_atlas + off_uv.y * 1.4), 0).x);
                float shadow_a = sh * 0.42 * prompt_a;
                bg = float4(0.02, 0.02, 0.03, shadow_a);
            }

            float out_a = saturate(glyph + bg.a * (1.0 - glyph));
            float3 out_rgb = (out_a > 1e-4)
                ? (fg * glyph + bg.rgb * bg.a * (1.0 - glyph)) / out_a
                : float3(0.0, 0.0, 0.0);
            u = float4(premultiplied != 0u ? out_rgb * out_a : out_rgb, out_a);
            break;
        }
    }

    float3 c = premultiplied != 0u ? g * (1.0 - u.a) + u.rgb : lerp(g, u.rgb, u.a);
    outp[id.xy] = float4(c, 1.0);
}
#endif
"#;

struct Pass {
    root: ID3D12RootSignature,
    pso_clear: ID3D12PipelineState,
    pso_detect: ID3D12PipelineState,
    pso_merge: ID3D12PipelineState,
    pso_compose: ID3D12PipelineState,
    views: ID3D12DescriptorHeap,
    cpu: usize,
    gpu: u64,
    step: u32,
    next: u32,
    atlas: ID3D12Resource,
    atlas_upload: Option<ID3D12Resource>,
    prompts: ID3D12Resource,
    /// The texture shown instead, by size.
    out: Option<(u64, u32, ID3D12Resource)>,
}
// SAFETY: D3D12 objects are free-threaded; the pass is used under a lock.
unsafe impl Send for Pass {}

/// The pass: not tried yet, could not be made, or made.
static PASS: Mutex<Option<Option<Pass>>> = Mutex::new(None);

fn compile(define: &[u8], entry: &[u8]) -> windows::core::Result<ID3DBlob> {
    use windows::Win32::Graphics::Direct3D::D3D_SHADER_MACRO;
    let macros = [
        D3D_SHADER_MACRO {
            Name: windows::core::PCSTR(define.as_ptr()),
            Definition: windows::core::s!("1"),
        },
        D3D_SHADER_MACRO::default(),
    ];
    let mut code: Option<ID3DBlob> = None;
    let mut errs: Option<ID3DBlob> = None;
    // SAFETY: static source/entry/macros live through the call.
    let res = unsafe {
        D3DCompile(
            SOURCE.as_ptr().cast(),
            SOURCE.len(),
            windows::core::s!("present planes"),
            Some(macros.as_ptr()),
            None,
            windows::core::PCSTR(entry.as_ptr()),
            windows::core::s!("cs_5_1"),
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
            tracing::warn!(entry = %String::from_utf8_lossy(entry), error = %msg, "compose shader compile failed");
        }
        return Err(e);
    }
    code.ok_or_else(windows::core::Error::empty)
}

fn make_pso(device: &ID3D12Device, root: &ID3D12RootSignature, blob: &ID3DBlob) -> windows::core::Result<ID3D12PipelineState> {
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

fn make(device: &ID3D12Device) -> windows::core::Result<Pass> {
    let b_clear = compile(b"CLEAR_HITS\0", b"clear_hits\0")?;
    let b_detect = compile(b"DETECT_HITS\0", b"detect_hits\0")?;
    let b_merge = compile(b"MERGE_HITS\0", b"merge_hits\0")?;
    let b_compose = compile(b"COMPOSE\0", b"main\0")?;

    // SAFETY: plain D3D12 object creation for compose pass resources.
    unsafe {
        let bytes = std::slice::from_raw_parts(b_compose.GetBufferPointer().cast::<u8>(), b_compose.GetBufferSize());
        let root: ID3D12RootSignature = device.CreateRootSignature(0, bytes)?;
        let pso_clear = make_pso(device, &root, &b_clear)?;
        let pso_detect = make_pso(device, &root, &b_detect)?;
        let pso_merge = make_pso(device, &root, &b_merge)?;
        let pso_compose = make_pso(device, &root, &b_compose)?;

        let kind = D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV;
        let views: ID3D12DescriptorHeap = device.CreateDescriptorHeap(&D3D12_DESCRIPTOR_HEAP_DESC {
            Type: kind,
            NumDescriptors: SLOTS * SLOTS_PER_FRAME,
            Flags: D3D12_DESCRIPTOR_HEAP_FLAG_SHADER_VISIBLE,
            NodeMask: 0,
        })?;

        let default_heap = D3D12_HEAP_PROPERTIES {
            Type: D3D12_HEAP_TYPE_DEFAULT,
            ..Default::default()
        };
        let upload_heap = D3D12_HEAP_PROPERTIES {
            Type: D3D12_HEAP_TYPE_UPLOAD,
            ..Default::default()
        };

        // 640x64 R8G8_UNORM texture for PC prompt icons (R=alpha coverage, G=ink).
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

        // Row pitch must be 256-byte aligned: 640*2 = 1280 bytes per row.
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

        // Raw UAV buffer for per-frame prompt candidates + merged hits (512 dwords = 2 KiB).
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

        Ok(Pass {
            cpu: views.GetCPUDescriptorHandleForHeapStart().ptr,
            gpu: views.GetGPUDescriptorHandleForHeapStart().ptr,
            step: device.GetDescriptorHandleIncrementSize(kind),
            root,
            pso_clear,
            pso_detect,
            pso_merge,
            pso_compose,
            views,
            next: 0,
            atlas,
            atlas_upload: Some(atlas_upload),
            prompts,
            out: None,
        })
    }
}

/// Whether a host texture's format is one this pass reads and writes
/// (8-bit RGBA, typeless or not).
fn rgba8(format: DXGI_FORMAT) -> bool {
    matches!(format.0, 27 | 28)
}

fn premultiplied() -> bool {
    static ON: std::sync::OnceLock<bool> = std::sync::OnceLock::new();
    *ON.get_or_init(|| std::env::var_os("VICTORY_PLANE_PREMULTIPLIED").is_some_and(|v| v == "1"))
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

fn vibrance_amount() -> f32 {
    static AMT: std::sync::OnceLock<f32> = std::sync::OnceLock::new();
    *AMT.get_or_init(|| {
        std::env::var("VICTORY_VIBRANCE")
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

/// Records, into the present's `list`, `ui` laid over `game` into a
/// texture of ours, and gives that texture, left in the copy-source
/// state. `game` is in the copy-source state (the present put it there);
/// `ui` is in `ui_state` and is left in it. `None` (nothing recorded) if
/// a format is not 8-bit RGBA or the pass could not be made.
pub(crate) fn record(
    device: &ID3D12Device,
    list: &ID3D12GraphicsCommandList,
    game: &ID3D12Resource,
    ui: &ID3D12Resource,
    ui_state: u32,
) -> Option<ID3D12Resource> {
    // SAFETY: live resources; plain queries.
    let (gd, ud) = unsafe { (game.GetDesc(), ui.GetDesc()) };
    if !rgba8(gd.Format) || !rgba8(ud.Format) {
        static ONCE: std::sync::Once = std::sync::Once::new();
        ONCE.call_once(|| {
            tracing::warn!(game = gd.Format.0, ui = ud.Format.0, "present planes not laid over each other: a format this pass does not read");
        });
        return None;
    }
    let mut kept = PASS.lock().unwrap_or_else(|e| e.into_inner());
    if kept.is_none() {
        *kept = Some(match make(device) {
            Ok(p) => {
                tracing::info!(
                    premultiplied = premultiplied(),
                    sharpen = sharpen_amount(),
                    vibrance = vibrance_amount(),
                    pc_prompts = pc_prompts_enabled(),
                    "present planes: the interface is laid over the game"
                );
                Some(p)
            }
            Err(e) => {
                tracing::warn!(error = %e, "present planes: the pass that lays them over each other could not be made");
                None
            }
        });
    }
    let pass = kept.as_mut()?.as_mut()?;
    let (w, h) = (gd.Width, gd.Height);
    if pass.out.as_ref().is_none_or(|o| (o.0, o.1) != (w, h)) {
        let heap = D3D12_HEAP_PROPERTIES {
            Type: D3D12_HEAP_TYPE_DEFAULT,
            ..Default::default()
        };
        let desc = D3D12_RESOURCE_DESC {
            Dimension: D3D12_RESOURCE_DIMENSION_TEXTURE2D,
            Width: w,
            Height: h,
            DepthOrArraySize: 1,
            MipLevels: 1,
            Format: DXGI_FORMAT_R8G8B8A8_UNORM,
            SampleDesc: DXGI_SAMPLE_DESC { Count: 1, Quality: 0 },
            Layout: D3D12_TEXTURE_LAYOUT_UNKNOWN,
            Flags: D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS,
            ..Default::default()
        };
        let mut out: Option<ID3D12Resource> = None;
        // SAFETY: plain creation of a device-local texture.
        unsafe {
            device
                .CreateCommittedResource(&heap, D3D12_HEAP_FLAG_NONE, &desc, D3D12_RESOURCE_STATE_COPY_SOURCE, None, &mut out)
                .ok()?;
        }
        pass.out = Some((w, h, out?));
    }

    let read = D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE;
    let t = crate::host::transition;

    // One-time GPU upload of the 640x64 R8G8 PC prompts atlas on the first frame.
    if let Some(upload) = pass.atlas_upload.take() {
        let row_pitch = (ATLAS_W * ATLAS_BPP + 255) & !255;
        let dst_loc = D3D12_TEXTURE_COPY_LOCATION {
            pResource: unsafe { std::mem::transmute_copy(&pass.atlas) },
            Type: D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX,
            Anonymous: D3D12_TEXTURE_COPY_LOCATION_0 { SubresourceIndex: 0 },
        };
        let src_loc = D3D12_TEXTURE_COPY_LOCATION {
            pResource: unsafe { std::mem::transmute_copy(&upload) },
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
        // SAFETY: valid open list and resources; upload buffer stays alive until drop at process exit via leak.
        unsafe {
            list.CopyTextureRegion(&dst_loc, 0, 0, 0, &src_loc, None);
            let bar = [t(&pass.atlas, D3D12_RESOURCE_STATE_COPY_DEST, read)];
            list.ResourceBarrier(&bar);
            std::mem::forget(upload);
            std::mem::forget(dst_loc);
            std::mem::forget(src_loc);
        }
    }

    let out = pass.out.as_ref()?.2.clone();
    let first = (pass.next % SLOTS) * SLOTS_PER_FRAME;
    pass.next = pass.next.wrapping_add(1);
    let cpu = |i: u32| D3D12_CPU_DESCRIPTOR_HANDLE { ptr: pass.cpu + ((first + i) * pass.step) as usize };
    let gpu = D3D12_GPU_DESCRIPTOR_HANDLE { ptr: pass.gpu + u64::from(first * pass.step) };
    let srv_rgba = D3D12_SHADER_RESOURCE_VIEW_DESC {
        Format: DXGI_FORMAT_R8G8B8A8_UNORM,
        ViewDimension: D3D12_SRV_DIMENSION_TEXTURE2D,
        Shader4ComponentMapping: D3D12_DEFAULT_SHADER_4_COMPONENT_MAPPING,
        Anonymous: D3D12_SHADER_RESOURCE_VIEW_DESC_0 {
            Texture2D: D3D12_TEX2D_SRV {
                MostDetailedMip: 0,
                MipLevels: 1,
                ..Default::default()
            },
        },
    };
    let srv_r8 = D3D12_SHADER_RESOURCE_VIEW_DESC {
        Format: DXGI_FORMAT_R8G8_UNORM,
        ViewDimension: D3D12_SRV_DIMENSION_TEXTURE2D,
        Shader4ComponentMapping: D3D12_DEFAULT_SHADER_4_COMPONENT_MAPPING,
        Anonymous: D3D12_SHADER_RESOURCE_VIEW_DESC_0 {
            Texture2D: D3D12_TEX2D_SRV {
                MostDetailedMip: 0,
                MipLevels: 1,
                ..Default::default()
            },
        },
    };
    let uav_out = D3D12_UNORDERED_ACCESS_VIEW_DESC {
        Format: DXGI_FORMAT_R8G8B8A8_UNORM,
        ViewDimension: D3D12_UAV_DIMENSION_TEXTURE2D,
        Anonymous: D3D12_UNORDERED_ACCESS_VIEW_DESC_0 {
            Texture2D: D3D12_TEX2D_UAV { MipSlice: 0, PlaneSlice: 0 },
        },
    };
    let uav_raw = D3D12_UNORDERED_ACCESS_VIEW_DESC {
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

    let ui_state = D3D12_RESOURCE_STATES(ui_state as i32);
    let mut before = vec![
        t(game, D3D12_RESOURCE_STATE_COPY_SOURCE, read),
        t(&out, D3D12_RESOURCE_STATE_COPY_SOURCE, D3D12_RESOURCE_STATE_UNORDERED_ACCESS),
    ];
    let mut after = vec![
        t(game, read, D3D12_RESOURCE_STATE_COPY_SOURCE),
        t(&out, D3D12_RESOURCE_STATE_UNORDERED_ACCESS, D3D12_RESOURCE_STATE_COPY_SOURCE),
    ];
    if ui_state != read {
        before.push(t(ui, ui_state, read));
        after.push(t(ui, read, ui_state));
    }

    let do_prompts = pc_prompts_enabled() && !REAL_PAD_CONNECTED.load(Ordering::Relaxed);
    let constants: [u32; 6] = [
        w as u32,
        h,
        u32::from(premultiplied()),
        sharpen_amount().to_bits(),
        u32::from(do_prompts),
        vibrance_amount().to_bits(),
    ];

    let uav_barrier = [D3D12_RESOURCE_BARRIER {
        Type: D3D12_RESOURCE_BARRIER_TYPE_UAV,
        Flags: D3D12_RESOURCE_BARRIER_FLAG_NONE,
        Anonymous: D3D12_RESOURCE_BARRIER_0 {
            UAV: std::mem::ManuallyDrop::new(D3D12_RESOURCE_UAV_BARRIER {
                pResource: unsafe { std::mem::transmute_copy(&pass.prompts) },
            }),
        },
    }];

    // SAFETY: open present list; slots of our heap; live resources and objects.
    unsafe {
        device.CreateShaderResourceView(game, Some(&srv_rgba), cpu(0));
        device.CreateShaderResourceView(ui, Some(&srv_rgba), cpu(1));
        device.CreateShaderResourceView(&pass.atlas, Some(&srv_r8), cpu(2));
        device.CreateUnorderedAccessView(&out, None::<&ID3D12Resource>, Some(&uav_out), cpu(3));
        device.CreateUnorderedAccessView(&pass.prompts, None::<&ID3D12Resource>, Some(&uav_raw), cpu(4));

        list.ResourceBarrier(&before);
        list.SetDescriptorHeaps(&[Some(pass.views.clone())]);
        list.SetComputeRootSignature(&pass.root);
        list.SetComputeRoot32BitConstants(0, 6, constants.as_ptr().cast(), 0);
        list.SetComputeRootDescriptorTable(1, gpu);

        if do_prompts {
            list.SetPipelineState(&pass.pso_clear);
            list.Dispatch(1, 1, 1);
            list.ResourceBarrier(&uav_barrier);

            list.SetPipelineState(&pass.pso_detect);
            list.Dispatch((w as u32).div_ceil(16), h.div_ceil(16), 1);
            list.ResourceBarrier(&uav_barrier);

            list.SetPipelineState(&pass.pso_merge);
            list.Dispatch(1, 1, 1);
            list.ResourceBarrier(&uav_barrier);
        }

        list.SetPipelineState(&pass.pso_compose);
        list.Dispatch((w as u32).div_ceil(8), h.div_ceil(8), 1);
        list.ResourceBarrier(&after);

        // `ManuallyDrop` inside `uav_barrier` did not AddRef, so leaving it untouched avoids a spurious Release.
    }
    Some(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn compose_and_pc_prompt_shaders_compile_cleanly() {
        for (define, entry) in [
            (&b"CLEAR_HITS\0"[..], &b"clear_hits\0"[..]),
            (&b"DETECT_HITS\0"[..], &b"detect_hits\0"[..]),
            (&b"MERGE_HITS\0"[..], &b"merge_hits\0"[..]),
            (&b"COMPOSE\0"[..], &b"main\0"[..]),
        ] {
            let blob = compile(define, entry).expect("HLSL shader should compile");
            // SAFETY: valid compiled D3DBlob.
            assert!(unsafe { blob.GetBufferSize() } > 0);
        }
    }
}
