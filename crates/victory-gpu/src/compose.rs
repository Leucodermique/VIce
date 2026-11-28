//! Two presented planes as one image: the console's display puts the second
//! (the interface) over the first (the scene), each scaled to the screen.
//! Grand Theft Auto VI presents so on Series S.

use windows::Win32::Graphics::Direct3D::Fxc::D3DCompile;
use windows::Win32::Graphics::Direct3D::ID3DBlob;
use windows::Win32::Graphics::Direct3D12::*;
use windows::Win32::Graphics::Dxgi::Common::*;
use windows::core::{PCSTR, s};

use crate::host::Host;

const ATLAS_W: u32 = 2304;
const ATLAS_H: u32 = 128;
const ATLAS_BYTES: &[u8] = include_bytes!("pc_prompts_atlas.bin");
const ATLAS_ROW: u32 = ATLAS_W * 2;
const PROMPT_BYTES: u64 = 2048;
const _: () = assert!(ATLAS_BYTES.len() == (ATLAS_ROW * ATLAS_H) as usize);

static REAL_PAD_CONNECTED: std::sync::atomic::AtomicBool =
    std::sync::atomic::AtomicBool::new(false);

/// Retain Xbox prompts whenever a real controller is connected.
pub fn set_real_pad_connected(connected: bool) {
    REAL_PAD_CONNECTED.store(connected, std::sync::atomic::Ordering::Relaxed);
}

fn amount(name: &str) -> f32 {
    std::env::var(name)
        .ok()
        .and_then(|s| s.parse::<f32>().ok())
        .filter(|v| v.is_finite())
        .unwrap_or(0.0)
        .clamp(0.0, 1.0)
}

// The NVIDIA module is private to victory-gpu; query the real controller
// here as well, so this file does not require a new cross-crate export.
fn xinput_connected() -> bool {
    type Query = unsafe extern "system" fn(u32, *mut [u32; 4]) -> u32;
    static QUERY: std::sync::OnceLock<Option<Query>> = std::sync::OnceLock::new();
    let query = QUERY.get_or_init(|| {
        use windows_sys::Win32::System::LibraryLoader::{
            GetProcAddress, LOAD_LIBRARY_SEARCH_SYSTEM32, LoadLibraryExW,
        };
        // SAFETY: only the Windows system directory is searched. Keep the
        // module loaded for this cached function pointer's process lifetime.
        unsafe {
            let module = LoadLibraryExW(
                windows::core::w!("xinput1_4.dll").as_ptr(),
                std::ptr::null_mut(),
                LOAD_LIBRARY_SEARCH_SYSTEM32,
            );
            if module.is_null() {
                return None;
            }
            let address = GetProcAddress(module, c"XInputGetState".as_ptr().cast())?;
            Some(std::mem::transmute::<
                unsafe extern "system" fn() -> isize,
                Query,
            >(address))
        }
    });
    query.is_some_and(|query| {
        // XINPUT_STATE is one packet DWORD plus a 12-byte gamepad state.
        let mut state = [0u32; 4];
        (0..4).any(|index| unsafe { query(index, &mut state) } == 0)
    })
}

fn pc_prompts_enabled() -> bool {
    std::env::var_os("VICTORY_PC_PROMPTS").is_some_and(|v| v != "0")
        && !REAL_PAD_CONNECTED.load(std::sync::atomic::Ordering::Relaxed)
        && !xinput_connected()
}

const SOURCE: &str = r#"
#define RS "RootConstants(num32BitConstants=7, b0), DescriptorTable(SRV(t0, numDescriptors=3)), UAV(u1), StaticSampler(s0, filter=FILTER_MIN_MAG_MIP_LINEAR, addressU=TEXTURE_ADDRESS_CLAMP, addressV=TEXTURE_ADDRESS_CLAMP)"

cbuffer Params : register(b0) {
    uint w;
    uint h;
    uint premultiplied;
    float sharpen;
    uint pc_prompts;
    float vibrance;
    uint reset_history;
};

Texture2D<float4> game : register(t0);
Texture2D<float4> ui : register(t1);
Texture2D<float2> atlas : register(t2);


// Prompt detection state (raw u32 buffer):
// dword 0: raw candidate count (0..64)
// dwords 1..1 + 64*4: raw candidates (cx, cy, packed_kind_flags, score_u32)
//   packed_kind_flags: bits 0..7 = icon_id (0..17), bits 8..15 = style (0=box, 1=floating, 2=hold inner, 3=dimmed, 4=QTE inner)
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

// Grey/partly transparent progress rings are not white glyph fill.
// Keep the face/letter tests strict; relax only this isolated annulus.
bool is_prompt_ring(float4 c) {
    float l = dot(c.rgb, LUMA);
    float hi = max(c.r, max(c.g, c.b));
    float lo = min(c.r, min(c.g, c.b));
    return c.a > 0.25 && l > 0.20 && hi - lo < 0.18;
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
// A -> E (tile 8), X -> SPACE (tile 6),
// B -> R (tile 5), Y -> F (tile 7); right-stick R -> C (tile 9).
int classify_white_circle_letter(float2 p, float scale, out float confidence) {
    confidence = 0.0;
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
    if (found < 3u) return -1;

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
    // A(8):  00100 / 01010 / 01010 / 11111 / 10001
    // X(6):  10001 / 01010 / 00100 / 01010 / 10001
    // R(0):  11110 / 10010 / 11100 / 10100 / 10010
    // B(5):  11110 / 10010 / 11110 / 10010 / 11110
    // Y(7):  10001 / 01010 / 00100 / 00100 / 00100
    // L(16): 10000 / 10000 / 10000 / 10000 / 11111
    static const uint MASKS[6] = {
        0x11FA944u, // A -> 8 (E)
        0x1151151u, // X -> 6 (SPACE)
        0x0929D2Fu, // R -> 9 (C)
        0x0F4BD2Fu, // B -> 5 (R)
        0x0421151u, // Y -> 7 (F)
        0x1F08421u  // L -> 16 (ZQSD)
    };
    static const int IDS[6] = { 8, 6, 9, 5, 7, 16 };

    int best_id = 8;
    float best_sc = -1e9;
    float next_sc = -1e9;
    for (int t = 0; t < 6; t++) {
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
            next_sc = best_sc;
            best_sc = sc;
            best_id = IDS[t];
        } else {
            next_sc = max(next_sc, sc);
        }
    }
    float vec_norm2 = 0.0;
    for (int k = 0; k < 25; k++) vec_norm2 += vec[k] * vec[k];
    confidence = best_sc / sqrt(max(vec_norm2, 1e-4));
    float separation = (best_sc - next_sc) / sqrt(max(vec_norm2, 1e-4));
    // Reject weak or ambiguous glyphs in every floating region, including
    // subtitles. A letter in ordinary text must not default to an icon.
    if (confidence < 0.65 || separation < 0.12) return -1;
    return best_id;
}

// World-space objective buttons are admitted only beside the compact pink
// objective dot (e.g. (Y) ARGENT). Never scan arbitrary scene discs as buttons.
bool is_objective_pink(float4 c) {
    return c.a > 0.72 && c.r > 0.62 && c.b > 0.40 &&
        c.r - c.g > 0.20 && c.b - c.g > 0.12;
}

bool has_objective_dot(float2 p, float scale) {
    float2 dot_pos = p + float2(40.0 * scale, 0.0);
    if (!is_objective_pink(sample_ui_px(dot_pos))) return false;
    float r = 4.0 * scale;
    uint pink = is_objective_pink(sample_ui_px(dot_pos + float2(r, 0.0))) ? 1u : 0u;
    pink += is_objective_pink(sample_ui_px(dot_pos - float2(r, 0.0))) ? 1u : 0u;
    pink += is_objective_pink(sample_ui_px(dot_pos + float2(0.0, r))) ? 1u : 0u;
    pink += is_objective_pink(sample_ui_px(dot_pos - float2(0.0, r))) ? 1u : 0u;
    return pink >= 3u;
}

// 8x8 luminance templates measured on the inner faces in audit 057/060.
// ZNCC removes brightness/contrast; ring geometry and alpha are checked first.
float face_match(float2 p, float scale, uint kind) {
    static const float LT[64] = { 0.9765,0.9765,0.9765,0.9765,0.9765,0.9765,0.9765,0.9765,0.8536,0.5257,0.9386,0.8545,0.4590,0.4163,0.4251,0.5852,0.7667,0.2078,0.9137,0.9124,0.6837,0.2255,0.4588,0.7660,0.7667,0.2078,0.9137,0.9765,0.9536,0.3196,0.6503,0.9739,0.7667,0.2046,0.8967,0.9595,0.9508,0.3196,0.6503,0.9739,0.7667,0.1196,0.4196,0.4928,0.8915,0.3196,0.6503,0.9739,0.8954,0.6111,0.6020,0.6362,0.9248,0.7203,0.8501,0.9754,0.9765,0.9765,0.9765,0.9765,0.9765,0.9765,0.9705,0.7676 };
    static const float QTE[64] = { 0.8828,0.9521,0.9529,0.9529,0.9529,0.9529,0.9521,0.9155,0.9512,0.8610,0.5176,0.7800,0.8048,0.5294,0.8418,0.9521,0.9529,0.9203,0.5176,0.3124,0.3817,0.4392,0.9072,0.9529,0.9529,0.9512,0.8614,0.2793,0.2279,0.8379,0.9503,0.9529,0.9529,0.9473,0.7843,0.2314,0.2166,0.7124,0.9434,0.9529,0.9529,0.8784,0.3451,0.4667,0.5948,0.2275,0.8340,0.9529,0.9495,0.8776,0.6954,0.8763,0.8998,0.7059,0.8370,0.9477,0.8329,0.9486,0.9529,0.9529,0.9529,0.9529,0.9521,0.9007 };
    float sum_x = 0.0, sum_t = 0.0, sum_xx = 0.0, sum_tt = 0.0, sum_xt = 0.0;
    [loop]
    for (uint i = 0u; i < 64u; i++) {
        float2 off = float2(-9.0 + float(i % 8u) * (18.0 / 7.0),
                           -9.0 + float(i / 8u) * (18.0 / 7.0)) * scale;
        float x = dot(sample_ui_px(p + off).rgb, LUMA);
        float t = kind == 0u ? LT[i] : QTE[i];
        sum_x += x; sum_t += t;
        sum_xx += x*x; sum_tt += t*t; sum_xt += x*t;
    }
    float variance = sum_xx - sum_x*sum_x / 64.0;
    if (variance < 0.05) return -1.0;
    return (sum_xt - sum_x*sum_t / 64.0) /
        sqrt(max(variance * (sum_tt - sum_t*sum_t / 64.0), 1e-5));
}

float locate_face(float2 p, float scale, uint kind, out float2 located) {
    float best = -1.0;
    located = p;
    [loop]
    for (int dy = -1; dy <= 1; dy++) {
        [loop]
        for (int dx = -1; dx <= 1; dx++) {
            float2 q = p + float2(float(dx), float(dy)) * (1.5 * scale);
            float score = face_match(q, scale, kind);
            if (score > best) { best = score; located = q; }
        }
    }
    return best;
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
    if (reset_history != 0u) prompts.Store(320 * 4, 0u);
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

    // The world-space ARGENT objective can move beside the radar or subtitles.
    // Its compact pink marker is mandatory everywhere, including those regions.
    bool objective = has_objective_dot(p, scale);
    if (p.x < float(w) * 0.24 && p.y > float(h) * 0.68 && !objective) {
        return;
    }

    float4 center = sample_ui_px(p);

    // QTE X: an inner button plus the large independent progress circle.
    bool qte_region = p.x > float(w) * 0.55 && p.x < float(w) * 0.96 &&
        p.y > float(h) * 0.25 && p.y < float(h) * 0.90;
    if (qte_region && center.a > 0.72) {
        uint inner = 0u;
        for (uint q = 0u; q < 12u; q++) {
            float ang = float(q) * (TWO_PI / 12.0);
            if (is_prompt_disc(sample_ui_px(p + float2(cos(ang), sin(ang)) * (10.0 * scale)))) inner++;
        }
        if (inner >= 10u) {
            uint progress = 0u, outside = 0u;
            for (uint q = 0u; q < 24u; q++) {
                float ang = float(q) * (TWO_PI / 24.0);
                float2 d = float2(cos(ang), sin(ang));
                bool r0 = is_prompt_ring(sample_ui_px(p + d * (38.0 * scale)));
                bool r1 = is_prompt_ring(sample_ui_px(p + d * (41.0 * scale)));
                bool r2 = is_prompt_ring(sample_ui_px(p + d * (44.0 * scale)));
                if (r0 || r1 || r2) progress++;
                if (sample_ui_px(p + d * (50.0 * scale)).a < 0.18) outside++;
            }
            if (progress >= 20u && outside >= 20u) {
                float2 located;
                float confidence = locate_face(p, scale, 1u, located);
                if (confidence >= 0.84) {
                    push_candidate(located, 6u, 4u, confidence + 1.0);
                    return;
                }
            }
        }
    }

    // ---- MODE A: Floating interaction & objective prompts (no dark box) ----
    // Bottom-right ("PARLER (A)", "MENACER (X)", "YOLANDA / EMPLOYE ((LT))")
    // and bottom-center ("Maintenez [LT] pour viser.", "Appuyez sur (X) pour menacer.", "Utilisez (R)...").
    bool in_bottom_right = (p.x > float(w) * 0.72 && p.y > float(h) * 0.55);
    bool in_bottom_center = (p.x > float(w) * 0.25 && p.x < float(w) * 0.78 && p.y > float(h) * 0.85);
    bool in_objective_center = objective;
    if (in_bottom_right || in_bottom_center || in_objective_center) {
        // A1) Hold-ring prompt (bottom-right): crisp white ring at radius ~18px, transparent outside at ~25px.
        if (in_bottom_right && !objective) {
            bool outer_clear =
                sample_ui_px(p + float2(0.0, -25.0 * scale)).a < 0.18 &&
                sample_ui_px(p + float2(0.0,  25.0 * scale)).a < 0.18 &&
                sample_ui_px(p + float2(25.0 * scale, 0.0)).a < 0.18;
            if (outer_clear) {
                uint ring18 = 0u;
                uint ring_band = 0u;
                uint ring25_clear = 0u;
                for (uint k = 0u; k < 16u; k++) {
                    float ang = float(k) * (TWO_PI / 16.0);
                    float2 dir = float2(cos(ang), sin(ang));
                    bool r18 = is_prompt_ring(sample_ui_px(p + dir * (18.0 * scale)));
                    if (r18) ring18++;
                    // Larger hold rings in the audit have radius ~20px; cover
                    // the 2px detection-grid phase without relaxing the clear exterior.
                    if (r18 || is_prompt_ring(sample_ui_px(p + dir * (20.0 * scale))) ||
                        is_prompt_ring(sample_ui_px(p + dir * (22.0 * scale)))) ring_band++;
                    if (sample_ui_px(p + dir * (25.0 * scale)).a < 0.18) ring25_clear++;
                }
                if (ring_band >= 13u && ring25_clear >= 10u) {
                    float inner_w = 0.0;
                    uint inner_d = 0u;
                    float inner_min = 1.0, inner_max = 0.0;
                    float tl = 0.0, tr = 0.0, bl = 0.0, br = 0.0;
                    for (int j = 0; j < 6; j++) {
                        for (int i = 0; i < 6; i++) {
                            float2 off = float2(-9.0 + float(i) * (18.0 / 5.0), -9.0 + float(j) * (18.0 / 5.0)) * scale;
                            float4 s = sample_ui_px(p + off);
                            float v = is_white_glyph(s) ? 1.0 : 0.0;
                            inner_w += v;
                            if (i >= 1 && i <= 4 && j >= 1 && j <= 4) {
                                if (is_dark_ink(s)) inner_d++;
                                if (s.a > 0.72) {
                                    float l = dot(s.rgb, LUMA);
                                    inner_min = min(inner_min, l);
                                    inner_max = max(inner_max, l);
                                }
                            }
                            if (j < 2 && i < 2) tl += v;
                            if (j < 2 && i >= 4) tr += v;
                            if (j >= 4 && i < 2) bl += v;
                            if (j >= 4 && i >= 4) br += v;
                        }
                    }
                    float fill = inner_w / 36.0;
                    // A grey outer ring and black LT are the audit 057 case.
                    // Match the complete two-letter face, not a single L/R guess.
                    if (fill >= 0.42 && inner_max - inner_min > 0.16) {
                        float2 located;
                        float confidence = locate_face(p, scale, 0u, located);
                        if (confidence >= 0.86) {
                            push_candidate(located, 0u, 2u, confidence + 1.0);
                            return;
                        }
                    }
                    // A compact X QTE uses the same hold annulus. Match the
                    // complete X face before generic glyph recognition; bevel
                    // asymmetry alone mistakes an off-centre X for a mouse.
                    float2 x_face;
                    float x_confidence = locate_face(p, scale, 1u, x_face);
                    if (x_confidence >= 0.84) {
                        push_candidate(x_face, 6u, 4u, x_confidence + 1.0);
                        return;
                    }
                    if (fill >= 0.42 && inner_d >= 2u) {
                        float confidence;
                        int letter = classify_white_circle_letter(p, scale, confidence);
                        if (letter < 0) return;
                        push_candidate(p, uint(letter), 2u, fill);
                        return;
                    }
                }
            }
        }

        // A2) Floating LT/RT trigger in bottom-center subtitle prompts (e.g. "Maintenez [LT] pour viser.")
        if (in_bottom_center && !objective && center.a > 0.65) {
            float pad_f = 18.0 * scale;
            bool clear_top_bot = sample_ui_px(p + float2(0.0, -pad_f)).a < 0.15
                              && sample_ui_px(p + float2(0.0,  pad_f)).a < 0.15;
            // Reject nearby letters that only resemble a trigger when the
            // next button contributes their right-hand silhouette.
            bool clear_sides = sample_ui_px(p + float2(-17.0 * scale, 0)).a < 0.18 &&
                               sample_ui_px(p + float2(17.0 * scale, 0)).a < 0.18;
            if (clear_top_bot && clear_sides) {
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
            sample_ui_px(p + float2(0.0,  17.0 * scale)).a < 0.18 &&
            (sample_ui_px(p + float2(-17.0 * scale, 0.0)).a < 0.18 ||
             sample_ui_px(p + float2( 17.0 * scale, 0.0)).a < 0.18);
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
                    // The world objective's smaller Y face needs a radius
                    // band across the scan phase; its pink marker stays mandatory.
                    if (objective && !is_prompt_disc(rs))
                        rs = sample_ui_px(p + dir * (8.0 * scale));
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
                    if (dark_cnt >= (objective ? 1u : 2u) && dark_cnt <= 7u) {
                        float confidence;
                        float2 located = p;
                        int icon = classify_white_circle_letter(p, scale, confidence);
                        // A button may lie between the two-pixel scan centres.
                        // Refine only after all disc/isolation guards have passed.
                        if (icon < 0) {
                            [loop]
                            for (int oy = -1; oy <= 1; oy++) {
                                [loop]
                                for (int ox = -1; ox <= 1; ox++) {
                                    float2 q = p + float2(float(ox), float(oy));
                                    float score;
                                    int candidate = classify_white_circle_letter(q, scale, score);
                                    if (candidate >= 0 && (icon < 0 || score > confidence)) {
                                        icon = candidate;
                                        confidence = score;
                                        located = q;
                                    }
                                }
                            }
                        }
                        if (icon < 0) return;
                        // Only the observed Y objective template is enabled in
                        // the new central region; unknown glyphs remain untouched.
                        if (objective && icon != 7) return;
                        uint style = (ring_luma / float(ring10) < 0.65) ? 3u : 1u; // 3 = dimmed inactive button
                        push_candidate(located, uint(icon), style, float(ring10) / 12.0);
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

    // Reserve each confidently recognised QTE's entire annulus. Offset trigger
    // candidates and stale mouse history must never draw beside its inner X.
    float qte_r = 50.0 * scale;
    for (uint i = 0u; i < raw_n; i++) {
        uint4 a = prompts.Load4((1u + i * 4u) * 4u);
        if (((a.z >> 8) & 0xFFu) == 4u) continue;
        for (uint j = 0u; j < raw_n; j++) {
            uint4 q = prompts.Load4((1u + j * 4u) * 4u);
            if (((q.z >> 8) & 0xFFu) != 4u) continue;
            float2 d = float2(a.xy) - float2(q.xy);
            if (dot(d, d) <= qte_r*qte_r) used[i] = true;
        }
    }

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
                // Snap moving prompts to the current face. Interpolating the
                // world-space objective left a PC key behind its moving Y.
                if (style != 4u && !has_objective_dot(float2(cx, cy), scale)) {
                    cx = lerp(float(prev_box[p].x), cx, 0.50);
                    cy = lerp(float(prev_box[p].y), cy, 0.50);
                }
                if (style != 4u && prev_meta[p].x != icon && best_w < 880u && prev_meta[p].z >= 6u) {
                    icon = prev_meta[p].x;
                }
                break;
            }
        }

        float rad = (style == 2u || style == 4u) ? 14.5 : 15.0;
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
        bool obscured = false;
        for (uint q = 0u; q < out_n; q++) {
            if (out_meta[q].y != 4u) continue;
            float2 d = pc - float2(out_box[q].xy);
            if (dot(d, d) <= qte_r*qte_r) obscured = true;
        }
        if (obscured) continue;
        // Alpha alone can belong to text, a moving progress arc, or a new HUD.
        // Revalidate the face before retaining an unmatched prompt.
        if (pstyle == 4u) {
            float2 located;
            if (locate_face(pc, scale, 1u, located) < 0.84) continue;
        } else if (prev_meta[p].x == 7u) {
            if (!has_objective_dot(pc, scale)) continue;
        } else if (prev_meta[p].x <= 1u && pstyle == 2u) {
            float2 located;
            if (prev_meta[p].x != 0u || locate_face(pc, scale, 0u, located) < 0.86) continue;
        }
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
    float2 uv = (float2(p) + 0.5) / float2(w, h);
    float3 c = game.SampleLevel(linear_clamp, uv, 0).rgb;
    if (sharpen <= 0.001) return c;
    uint gw, gh;
    game.GetDimensions(gw, gh);
    float2 px = 1.0 / float2(gw, gh);
    float3 n = game.SampleLevel(linear_clamp, uv + float2(0, -px.y), 0).rgb;
    float3 s = game.SampleLevel(linear_clamp, uv + float2(0, px.y), 0).rgb;
    float3 e = game.SampleLevel(linear_clamp, uv + float2(px.x, 0), 0).rgb;
    float3 b = game.SampleLevel(linear_clamp, uv + float2(-px.x, 0), 0).rgb;
    float3 mn = min(c, min(min(n, s), min(e, b)));
    float3 mx = max(c, max(max(n, s), max(e, b)));
    float3 amp = saturate(min(mn, 1.0 - mx) / max(mx, 1e-4));
    float3 weight = sqrt(amp) * (-0.16 * sharpen);
    return saturate((c + (n + s + e + b) * weight) / (1.0 + 4.0 * weight));
}

// Vice City vitality color-grading pass (game plane only, never touches UI/HUD/prompts):
// 1) Micro shadow lift strictly in dark shadows (luma < 0.24) so indoor scenes keep
//    readability without ever brightening sunlit faces or skies.
// 2) Smart vibrance that enriches muted/mid-saturated colors while protecting skin tones
//    and bright highlights from clipping.
// 3) Gentle highlight rolloff + subtle contrast so sunlit scenes stay rich, never blown out.
float3 grade_game(float3 c) {
    if (vibrance <= 0.001) return c;
    float luma = dot(c, LUMA);
    float shadow_mask = smoothstep(0.015, 0.12, luma) * (1.0 - smoothstep(0.14, 0.30, luma));
    c += c * (0.08 * vibrance * shadow_mask);

    float hi = max(c.r, max(c.g, c.b));
    float lo = min(c.r, min(c.g, c.b));
    float sat = (hi > 1e-4) ? ((hi - lo) / hi) : 0.0;
    float l2 = dot(c, LUMA);
    float luma_guard = smoothstep(0.025, 0.12, l2) * (1.0 - smoothstep(0.78, 0.96, l2));
    float skin = saturate(1.0 - abs(c.r - c.g * 1.28) * 3.0) * smoothstep(0.05, 0.20, c.r - c.b);
    float vib_boost = vibrance * (1.0 - sat * 0.68) * luma_guard * (1.0 - 0.32 * skin);
    c = lerp(float3(l2, l2, l2), c, 1.0 + vib_boost * 1.25);

    // Subtle filmic contrast without pushing brights past 1.0
    float3 s_curve = c * c * (3.0 - 2.0 * c);
    c = lerp(c, s_curve, 0.12 * vibrance);
    return saturate(c);
}

struct V { float4 at : SV_Position; float2 uv : TEXCOORD0; };
[RootSignature(RS)]
V vs(uint id : SV_VertexID) {
    V v;
    v.uv = float2((id << 1) & 2, id & 2);
    v.at = float4(v.uv * float2(2, -2) + float2(-1, 1), 0, 1);
    return v;
}
[RootSignature(RS)]
float4 ps(V v) : SV_Target {
    uint3 id = uint3(uint2(v.at.xy), 0);
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
            if ((style == 2u || style == 4u) && dot(rel, rel) > 1.0) continue;

            float2 tile_uv = rel * 0.5 + 0.5;
            float u_atlas = (float(icon) + clamp(tile_uv.x, 0.004, 0.996)) / 18.0;
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
                float2 off_uv = float2(0.001, 0.018);
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
    return float4(saturate(c), 1.0);
}
#endif
"#;
pub(crate) struct Composer {
    root: ID3D12RootSignature,
    pso: ID3D12PipelineState,
    views: Vec<ID3D12DescriptorHeap>,
    clear: ID3D12PipelineState,
    detect: ID3D12PipelineState,
    merge: ID3D12PipelineState,
    atlas: ID3D12Resource,
    prompts: ID3D12Resource,
    upload: ID3D12Resource,
    initialized: bool,
    last_size: (u32, u32),
    had_prompts: bool,
    step: u32,
}
// SAFETY: D3D12 objects are free-threaded.
unsafe impl Send for Composer {}
// SAFETY: as above.
unsafe impl Sync for Composer {}

fn compile(part: &[u8], entry: PCSTR, target: PCSTR) -> windows::core::Result<ID3DBlob> {
    let mut code: Option<ID3DBlob> = None;
    let mut errors: Option<ID3DBlob> = None;
    let macros = [
        windows::Win32::Graphics::Direct3D::D3D_SHADER_MACRO {
            Name: PCSTR(part.as_ptr()),
            Definition: s!("1"),
        },
        windows::Win32::Graphics::Direct3D::D3D_SHADER_MACRO::default(),
    ];
    // SAFETY: the source and names live through the call; the out pointer is
    // ours.
    unsafe {
        D3DCompile(
            SOURCE.as_ptr().cast(),
            SOURCE.len(),
            s!("compose"),
            Some(macros.as_ptr()),
            None,
            entry,
            target,
            0,
            0,
            &mut code,
            Some(&mut errors),
        )?;
    }
    code.ok_or_else(windows::core::Error::empty)
}

impl Composer {
    pub(crate) fn new(host: &Host, format: DXGI_FORMAT) -> windows::core::Result<Self> {
        let (vs, ps) = (
            compile(b"COMPOSE\0", s!("vs"), s!("vs_5_1"))?,
            compile(b"COMPOSE\0", s!("ps"), s!("ps_5_1"))?,
        );
        // SAFETY: the blobs' own bytes, alive through the calls.
        unsafe {
            let bytes = |b: &ID3DBlob| {
                std::slice::from_raw_parts(b.GetBufferPointer().cast::<u8>(), b.GetBufferSize())
            };
            let device = &host.device;
            let root: ID3D12RootSignature = device.CreateRootSignature(0, bytes(&vs))?;
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
            let compute = |part, entry| -> windows::core::Result<ID3D12PipelineState> {
                let b = compile(part, entry, s!("cs_5_1"))?;
                device.CreateComputePipelineState(&D3D12_COMPUTE_PIPELINE_STATE_DESC {
                    pRootSignature: std::mem::transmute_copy(&root),
                    CS: code(&b),
                    ..Default::default()
                })
            };
            let clear = compute(&b"CLEAR_HITS\0"[..], s!("clear_hits"))?;
            let detect = compute(&b"DETECT_HITS\0"[..], s!("detect_hits"))?;
            let merge = compute(&b"MERGE_HITS\0"[..], s!("merge_hits"))?;
            let (atlas, prompts, upload) = prompt_resources(device)?;
            Ok(Composer {
                clear,
                detect,
                merge,
                atlas,
                prompts,
                upload,
                initialized: false,
                last_size: (0, 0),
                had_prompts: false,
                root,
                pso,
                views: Vec::new(),
                step: device
                    .GetDescriptorHandleIncrementSize(D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV),
            })
        }
    }

    /// Each presenter command-list slot owns its own pair of SRVs. The swap
    /// chain may have several outstanding presents; the slot is selected by
    /// the completion fence, and its descriptors are only overwritten on reuse.
    pub(crate) fn record(
        &mut self,
        host: &Host,
        slot: usize,
        list: &ID3D12GraphicsCommandList,
        planes: [(&ID3D12Resource, DXGI_FORMAT); 2],
        back: &ID3D12Resource,
        (width, height): (u32, u32),
    ) -> windows::core::Result<()> {
        self.record_with_prompts(
            host,
            slot,
            list,
            planes,
            back,
            (width, height),
            pc_prompts_enabled(),
        )
    }

    fn record_with_prompts(
        &mut self,
        host: &Host,
        slot: usize,
        list: &ID3D12GraphicsCommandList,
        planes: [(&ID3D12Resource, DXGI_FORMAT); 2],
        back: &ID3D12Resource,
        (width, height): (u32, u32),
        enable_prompts: bool,
    ) -> windows::core::Result<()> {
        while self.views.len() <= slot {
            let heap = unsafe {
                host.device
                    .CreateDescriptorHeap(&D3D12_DESCRIPTOR_HEAP_DESC {
                        Type: D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV,
                        NumDescriptors: 3,
                        Flags: D3D12_DESCRIPTOR_HEAP_FLAG_SHADER_VISIBLE,
                        NodeMask: 0,
                    })?
            };
            self.views.push(heap);
        }
        let views = self.views[slot].clone();
        // SAFETY: an open list; the views are made in slots no frame in flight
        // reads; live textures.
        unsafe {
            let (cpu, gpu) = (
                views.GetCPUDescriptorHandleForHeapStart(),
                views.GetGPUDescriptorHandleForHeapStart(),
            );
            for (i, plane) in planes.iter().enumerate() {
                let at = D3D12_CPU_DESCRIPTOR_HANDLE {
                    ptr: cpu.ptr + (i * self.step as usize),
                };
                let desc = D3D12_SHADER_RESOURCE_VIEW_DESC {
                    Format: plane.1,
                    ViewDimension: D3D12_SRV_DIMENSION_TEXTURE2D,
                    Shader4ComponentMapping: D3D12_DEFAULT_SHADER_4_COMPONENT_MAPPING,
                    Anonymous: D3D12_SHADER_RESOURCE_VIEW_DESC_0 {
                        Texture2D: D3D12_TEX2D_SRV {
                            MipLevels: 1,
                            ..Default::default()
                        },
                    },
                };
                host.device
                    .CreateShaderResourceView(Some(plane.0), Some(&desc), at);
            }
            let at = D3D12_CPU_DESCRIPTOR_HANDLE {
                ptr: cpu.ptr + 2 * self.step as usize,
            };
            let atlas_view = D3D12_SHADER_RESOURCE_VIEW_DESC {
                Format: DXGI_FORMAT_R8G8_UNORM,
                ViewDimension: D3D12_SRV_DIMENSION_TEXTURE2D,
                Shader4ComponentMapping: D3D12_DEFAULT_SHADER_4_COMPONENT_MAPPING,
                Anonymous: D3D12_SHADER_RESOURCE_VIEW_DESC_0 {
                    Texture2D: D3D12_TEX2D_SRV {
                        MipLevels: 1,
                        ..Default::default()
                    },
                },
            };
            host.device
                .CreateShaderResourceView(Some(&self.atlas), Some(&atlas_view), at);
            list.SetDescriptorHeaps(&[Some(views.clone())]);
            self.initialize(list);
            let do_prompts = width >= 96 && height >= 96 && enable_prompts;
            let constants = [
                width,
                height,
                1,
                amount("VICTORY_SHARPEN").to_bits(),
                u32::from(do_prompts),
                amount("VICTORY_VIBRANCE").to_bits(),
                u32::from(self.last_size != (width, height) || !self.had_prompts),
            ];
            if do_prompts {
                self.detect_prompts(list, planes[1].0, gpu, &constants);
            }
            self.last_size = (width, height);
            self.had_prompts = do_prompts;
            let rtv = host.rtv(Some(back), None);
            list.OMSetRenderTargets(1, Some(&rtv), false, None);
            list.SetDescriptorHeaps(&[Some(views.clone())]);
            list.SetGraphicsRootSignature(&self.root);
            list.SetPipelineState(&self.pso);
            list.SetGraphicsRoot32BitConstants(
                0,
                constants.len() as u32,
                constants.as_ptr().cast(),
                0,
            );
            list.SetGraphicsRootDescriptorTable(1, gpu);
            list.SetGraphicsRootUnorderedAccessView(2, self.prompts.GetGPUVirtualAddress());
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
                prompt_barrier(list);
            }
        }
        Ok(())
    }
}

fn prompt_resources(
    device: &ID3D12Device,
) -> windows::core::Result<(ID3D12Resource, ID3D12Resource, ID3D12Resource)> {
    let make = |ty, desc: &D3D12_RESOURCE_DESC, state| -> windows::core::Result<ID3D12Resource> {
        let mut out = None;
        // SAFETY: valid descriptions and an owned output pointer.
        unsafe {
            device.CreateCommittedResource(
                &D3D12_HEAP_PROPERTIES {
                    Type: ty,
                    ..Default::default()
                },
                D3D12_HEAP_FLAG_NONE,
                desc,
                state,
                None,
                &mut out,
            )?;
        }
        out.ok_or_else(windows::core::Error::empty)
    };
    let buffer = |bytes, flags| D3D12_RESOURCE_DESC {
        Dimension: D3D12_RESOURCE_DIMENSION_BUFFER,
        Width: bytes,
        Height: 1,
        DepthOrArraySize: 1,
        MipLevels: 1,
        SampleDesc: DXGI_SAMPLE_DESC {
            Count: 1,
            Quality: 0,
        },
        Layout: D3D12_TEXTURE_LAYOUT_ROW_MAJOR,
        Flags: flags,
        ..Default::default()
    };
    let atlas = make(
        D3D12_HEAP_TYPE_DEFAULT,
        &D3D12_RESOURCE_DESC {
            Dimension: D3D12_RESOURCE_DIMENSION_TEXTURE2D,
            Width: u64::from(ATLAS_W),
            Height: ATLAS_H,
            DepthOrArraySize: 1,
            MipLevels: 1,
            Format: DXGI_FORMAT_R8G8_UNORM,
            SampleDesc: DXGI_SAMPLE_DESC {
                Count: 1,
                Quality: 0,
            },
            ..Default::default()
        },
        D3D12_RESOURCE_STATE_COPY_DEST,
    )?;
    let prompts = make(
        D3D12_HEAP_TYPE_DEFAULT,
        &buffer(PROMPT_BYTES, D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS),
        D3D12_RESOURCE_STATE_COPY_DEST,
    )?;
    let upload = make(
        D3D12_HEAP_TYPE_UPLOAD,
        &buffer(
            ATLAS_BYTES.len() as u64 + PROMPT_BYTES,
            D3D12_RESOURCE_FLAG_NONE,
        ),
        D3D12_RESOURCE_STATE_GENERIC_READ,
    )?;
    // SAFETY: the mapped upload has exactly the checked atlas plus history bytes.
    unsafe {
        let mut ptr = std::ptr::null_mut();
        upload.Map(0, None, Some(&mut ptr))?;
        std::ptr::copy_nonoverlapping(ATLAS_BYTES.as_ptr(), ptr.cast(), ATLAS_BYTES.len());
        std::ptr::write_bytes(
            ptr.cast::<u8>().add(ATLAS_BYTES.len()),
            0,
            PROMPT_BYTES as usize,
        );
        upload.Unmap(0, None);
    }
    Ok((atlas, prompts, upload))
}

fn prompt_barrier(list: &ID3D12GraphicsCommandList) {
    // SAFETY: global UAV barrier orders prompt writes and the pixel shader's reads.
    unsafe {
        list.ResourceBarrier(&[D3D12_RESOURCE_BARRIER {
            Type: D3D12_RESOURCE_BARRIER_TYPE_UAV,
            ..Default::default()
        }]);
    }
}

impl Composer {
    fn initialize(&mut self, list: &ID3D12GraphicsCommandList) {
        if self.initialized {
            return;
        }
        // SAFETY: self retains both resources and upload for all presents.
        unsafe {
            let mut dst = D3D12_TEXTURE_COPY_LOCATION {
                pResource: std::mem::ManuallyDrop::new(Some(self.atlas.clone())),
                Type: D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX,
                ..Default::default()
            };
            let mut src = D3D12_TEXTURE_COPY_LOCATION {
                pResource: std::mem::ManuallyDrop::new(Some(self.upload.clone())),
                Type: D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT,
                Anonymous: D3D12_TEXTURE_COPY_LOCATION_0 {
                    PlacedFootprint: D3D12_PLACED_SUBRESOURCE_FOOTPRINT {
                        Offset: 0,
                        Footprint: D3D12_SUBRESOURCE_FOOTPRINT {
                            Format: DXGI_FORMAT_R8G8_UNORM,
                            Width: ATLAS_W,
                            Height: ATLAS_H,
                            Depth: 1,
                            RowPitch: ATLAS_ROW,
                        },
                    },
                },
            };
            list.CopyTextureRegion(&dst, 0, 0, 0, &src, None);
            std::mem::ManuallyDrop::drop(&mut dst.pResource);
            std::mem::ManuallyDrop::drop(&mut src.pResource);
            list.CopyBufferRegion(
                &self.prompts,
                0,
                &self.upload,
                ATLAS_BYTES.len() as u64,
                PROMPT_BYTES,
            );
            list.ResourceBarrier(&[
                crate::host::transition(
                    &self.atlas,
                    D3D12_RESOURCE_STATE_COPY_DEST,
                    D3D12_RESOURCE_STATE_PIXEL_SHADER_RESOURCE,
                ),
                crate::host::transition(
                    &self.prompts,
                    D3D12_RESOURCE_STATE_COPY_DEST,
                    D3D12_RESOURCE_STATE_UNORDERED_ACCESS,
                ),
            ]);
        }
        self.initialized = true;
    }

    fn detect_prompts(
        &self,
        list: &ID3D12GraphicsCommandList,
        ui: &ID3D12Resource,
        views: D3D12_GPU_DESCRIPTOR_HANDLE,
        constants: &[u32; 7],
    ) {
        let pixel = D3D12_RESOURCE_STATE_PIXEL_SHADER_RESOURCE;
        let compute = D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE;
        // SAFETY: presenter leaves both planes in pixel SRV state; restore it
        // before the existing graphics composition and present transitions.
        unsafe {
            list.ResourceBarrier(&[crate::host::transition(ui, pixel, compute)]);
            prompt_barrier(list);
            list.SetComputeRootSignature(&self.root);
            list.SetComputeRoot32BitConstants(
                0,
                constants.len() as u32,
                constants.as_ptr().cast(),
                0,
            );
            list.SetComputeRootDescriptorTable(1, views);
            list.SetComputeRootUnorderedAccessView(2, self.prompts.GetGPUVirtualAddress());
            list.SetPipelineState(&self.clear);
            list.Dispatch(1, 1, 1);
            prompt_barrier(list);
            list.SetPipelineState(&self.detect);
            list.Dispatch(constants[0].div_ceil(16), constants[1].div_ceil(16), 1);
            prompt_barrier(list);
            list.SetPipelineState(&self.merge);
            list.Dispatch(1, 1, 1);
            prompt_barrier(list);
            list.ResourceBarrier(&[crate::host::transition(ui, compute, pixel)]);
        }
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
        // A half-red base at one resolution. The overlay at output resolution
        // contains transparent, half-opaque green and fully opaque blue pixels.
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

#[cfg(test)]
mod shader_tests {
    use super::*;

    #[test]
    fn pc_prompt_and_graphics_shaders_compile() {
        for (part, entry, target) in [
            (&b"CLEAR_HITS\0"[..], s!("clear_hits"), s!("cs_5_1")),
            (&b"DETECT_HITS\0"[..], s!("detect_hits"), s!("cs_5_1")),
            (&b"MERGE_HITS\0"[..], s!("merge_hits"), s!("cs_5_1")),
            (&b"COMPOSE\0"[..], s!("vs"), s!("vs_5_1")),
            (&b"COMPOSE\0"[..], s!("ps"), s!("ps_5_1")),
        ] {
            assert!(unsafe { compile(part, entry, target).unwrap().GetBufferSize() } > 0);
        }
    }
}

#[cfg(all(test, feature = "diagnostics"))]
mod prompt_tests {
    use super::*;

    // Quantized neutral UI silhouettes from audit 057/060; background is removed.
    const LT: &[&str] = &[
        "...................................",
        "...................................",
        "...................................",
        "...................................",
        "...................................",
        "............2344444432.............",
        ".........12344444444444310.........",
        ".........34444322223344442.........",
        ".........222110000001122232........",
        ".......325666666666666664232.......",
        "......33389999999999999985.4.......",
        ".....344269999999999999996.43......",
        ".....443.59999999999999996.442.....",
        "....344..49866898655555796.343.....",
        "....343..39822898311011386.244.....",
        "....442..39822898763146796..442....",
        "....44...28822899995159996..343....",
        "....44...28822899995159996..343....",
        "....44...28822899995159996..343....",
        "....44...28822788995159995..343....",
        "....44...38821444685159984..442....",
        "....443..39831112585269983..44.....",
        "....343..4998777789888996..344.....",
        ".....443.5999999999999983..443.....",
        ".....344.699999999999984..344......",
        "......43279999999999873..3442......",
        "......2337888888887641..3443.......",
        "......................24443........",
        "........2444432.....344443.........",
        "..........34444444444443...........",
        "............3344444443.............",
        ".................2.................",
        "...................................",
        "...................................",
        "...................................",
        "...................................",
    ];
    const QTE: &[&str] = &[
        ".....................................................................",
        ".....................................................................",
        ".....................................................................",
        ".....................................................................",
        ".....................................................................",
        ".....................................................................",
        "...............................222222................................",
        "..........................2333444444443332...........................",
        ".......................233334444455554444433.........................",
        ".....................33333445443333333345555432......................",
        "...................3333454432............2445543.....................",
        ".................233345432..................345543...................",
        "................3333543.......................34553..................",
        "...............333453...........................35542................",
        "..............333453.............................24542...............",
        ".............33354.................................4642..............",
        "............33354...................................4642.............",
        "...........33354.....................................4542............",
        "..........24344.......................................553............",
        "..........33442..................24432................3543...........",
        ".........23353...............23445788865...............4542..........",
        ".........3354..............344444578999985..............543..........",
        ".........3452............2344433234567899863............3532.........",
        "........3353............34443.........3589974............443.........",
        "........444............3443.....34432...479974...........3432........",
        ".......3454...........2443...3678888874..268973..........2543........",
        ".......4442...........343...5789999998863.36985...........443........",
        ".......444...........343...589999999999874.37984..........453........",
        ".......443...........343..48999999999999862.5886..........3432.......",
        "......3542..........343..379987678876789985.37973..........443.......",
        "......355...........343..5899863377436899873.5984..........543.......",
        "......354...........343..7999985234247999985.4895..........443.......",
        "......354...........342.37999987311368999986.3896..........443.......",
        "......344...........442.38999998621489999987.27962.........443.......",
        "......344...........442.38999998411379999987..675..........443.......",
        "......354...........342.37999986223258999986..443..........443.......",
        "......354...........343..6999873256326899985..342..........453.......",
        "......355...........343..4899853588634799874..43...........453.......",
        "......2552..........2442.2789888899888899862.343...........453.......",
        ".......443...........343..47999999999999873..443..........244........",
        ".......444...........2443..478999999999864..343...........354........",
        ".......3443...........3432.136889999987631.3432...........353........",
        ".......3444............3432..24678877632..3443............552........",
        "........4342............3443..112232211..3443............254.........",
        "........3443.............34433........234433.............453.........",
        ".........4342.............34444333333344432.............354..........",
        ".........3344...............3344444444432...............453..........",
        ".........24343................23333332.................364...........",
        "..........33343.......................................3552...........",
        "...........3344.......................................553............",
        "...........23344.....................................453.............",
        "............24344...................................464..............",
        ".............333442...............................2464...............",
        "..............243442.............................2564................",
        "...............233453...........................4653.................",
        ".................344543.......................35653..................",
        "..................2345543...................35653....................",
        "....................34455542.............3455543.....................",
        "......................234455554333333345555432.......................",
        ".........................334444445554455443..........................",
        "............................233333333332.............................",
        ".....................................................................",
        ".....................................................................",
        ".....................................................................",
        ".....................................................................",
        ".....................................................................",
        ".....................................................................",
        ".....................................................................",
        ".....................................................................",
    ];
    const SUBTITLE: &[&str] = &[
        "..........................................................................................",
        "..........................................................................................",
        "..........................................................................................",
        "..........................................................................................",
        "..........................................................................................",
        "..........................................................................................",
        "..................................................99999...................................",
        "................................................999999999.................................",
        "..............................................9999999999999...............................",
        ".............................................999999999990999..............................",
        ".............................................999909999900099..............................",
        ".999999...999..999..999999..................99990009990009999.............................",
        "99999999..999..999..999999..................99999000990099999.............................",
        "99999999..999..999..99999..................9999999009000999999............................",
        "9999999...999..999..999....................9999999000009999999............................",
        ".9999999..999..999..999....................9999999900099999999............................",
        "..9999999.999..999..999....................9999999000009999999............................",
        "999999999.99999999..999....................9999999009000999999............................",
        "99999999..99999999..999.....................99999000990099999.............................",
        ".9999999..99999999..999.....................99990009990009999.............................",
        ".............................................999909999900099..............................",
        ".............................................999999999990999..............................",
        "..............................................9999999999999...............................",
        "................................................999999999.................................",
        "..................................................99999...................................",
        "..........................................................................................",
        "..........................................................................................",
        "..........................................................................................",
        "..........................................................................................",
        "..........................................................................................",
        "..........................................................................................",
    ];

    fn stamp(pixels: &mut [u8], x0: usize, y0: usize, rows: &[&str]) {
        for (y, row) in rows.iter().enumerate() {
            for (x, c) in row.bytes().enumerate() {
                if c == b'.' {
                    continue;
                }
                let value = ((u16::from(c - b'0') * 255 + 4) / 9) as u8;
                let at = ((y0 + y) * 1280 + x0 + x) * 4;
                pixels[at..at + 4].copy_from_slice(&[value, value, value, 255]);
            }
        }
    }

    // Measured Y face from pre-overlay audit; compact pink marker added below.
    const OBJECTIVE_Y: &[&str] = &[
        ".....................",
        ".....................",
        ".....................",
        ".......9999999.......",
        "......999999999......",
        ".....99999999999.....",
        "....9867998668999....",
        "...998327962489999...",
        "...999614732799999...",
        "...999841315999999...",
        "...999972138999999...",
        "...999994169999999...",
        "...999994169999999...",
        "...999994169999999...",
        "....9999637999999....",
        ".....99999999999.....",
        "......999999999......",
        ".......9999999.......",
        ".....................",
        ".....................",
        ".....................",
    ];
    fn render_fixture(
        host: &Host,
        compositor: &mut Composer,
        pixels: &[u8],
        seed: Option<&[u32; 512]>,
    ) -> (Vec<[u32; 6]>, Vec<u8>) {
        let format = DXGI_FORMAT_R8G8B8A8_UNORM;
        let make = |width, height, state, flags| {
            let mut texture = None;
            let desc = D3D12_RESOURCE_DESC {
                Dimension: D3D12_RESOURCE_DIMENSION_TEXTURE2D,
                Width: width,
                Height: height,
                DepthOrArraySize: 1,
                MipLevels: 1,
                Format: format,
                SampleDesc: DXGI_SAMPLE_DESC {
                    Count: 1,
                    Quality: 0,
                },
                Flags: flags,
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
                        state,
                        None,
                        &mut texture,
                    )
                    .unwrap();
            }
            texture.unwrap()
        };
        let rt = D3D12_RESOURCE_STATE_RENDER_TARGET;
        let read = D3D12_RESOURCE_STATE_PIXEL_SHADER_RESOURCE;
        let base = make(1, 1, rt, D3D12_RESOURCE_FLAG_ALLOW_RENDER_TARGET);
        let ui = make(
            1280,
            700,
            D3D12_RESOURCE_STATE_COPY_DEST,
            D3D12_RESOURCE_FLAG_NONE,
        );
        let output = make(1280, 700, rt, D3D12_RESOURCE_FLAG_ALLOW_RENDER_TARGET);
        let (upload, ptr) = host.upload_buffer(pixels.len() as u64).unwrap();
        unsafe {
            std::ptr::copy_nonoverlapping(pixels.as_ptr(), ptr, pixels.len());
        }
        let list = host.new_list().unwrap();
        let cmds = &list.list;
        let prompt_readback = host.read_back_buffer(PROMPT_BYTES).unwrap();
        let (image_readback, footprint);
        let seed_upload = seed.map(|words| {
            let (upload, ptr) = host.upload_buffer(PROMPT_BYTES).unwrap();
            unsafe {
                std::ptr::copy_nonoverlapping(
                    words.as_ptr().cast::<u8>(),
                    ptr,
                    PROMPT_BYTES as usize,
                );
            }
            upload
        });
        unsafe {
            cmds.ClearRenderTargetView(host.rtv(Some(&base), None), &[0., 0., 0., 1.], None);
            let mut src = D3D12_TEXTURE_COPY_LOCATION {
                pResource: std::mem::ManuallyDrop::new(Some(upload.clone())),
                Type: D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT,
                Anonymous: D3D12_TEXTURE_COPY_LOCATION_0 {
                    PlacedFootprint: D3D12_PLACED_SUBRESOURCE_FOOTPRINT {
                        Offset: 0,
                        Footprint: D3D12_SUBRESOURCE_FOOTPRINT {
                            Format: format,
                            Width: 1280,
                            Height: 700,
                            Depth: 1,
                            RowPitch: 5120,
                        },
                    },
                },
            };
            let mut dst = D3D12_TEXTURE_COPY_LOCATION {
                pResource: std::mem::ManuallyDrop::new(Some(ui.clone())),
                Type: D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX,
                ..Default::default()
            };
            cmds.CopyTextureRegion(&dst, 0, 0, 0, &src, None);
            std::mem::ManuallyDrop::drop(&mut src.pResource);
            std::mem::ManuallyDrop::drop(&mut dst.pResource);
            cmds.ResourceBarrier(&[
                crate::host::transition(&base, rt, read),
                crate::host::transition(&ui, D3D12_RESOURCE_STATE_COPY_DEST, read),
            ]);
            if let Some(upload) = &seed_upload {
                cmds.ResourceBarrier(&[crate::host::transition(
                    &compositor.prompts,
                    D3D12_RESOURCE_STATE_UNORDERED_ACCESS,
                    D3D12_RESOURCE_STATE_COPY_DEST,
                )]);
                cmds.CopyBufferRegion(&compositor.prompts, 0, upload, 0, PROMPT_BYTES);
                cmds.ResourceBarrier(&[crate::host::transition(
                    &compositor.prompts,
                    D3D12_RESOURCE_STATE_COPY_DEST,
                    D3D12_RESOURCE_STATE_UNORDERED_ACCESS,
                )]);
            }
            compositor
                .record_with_prompts(
                    host,
                    0,
                    cmds,
                    [(&base, format), (&ui, format)],
                    &output,
                    (1280, 700),
                    true,
                )
                .unwrap();
            cmds.ResourceBarrier(&[
                crate::host::transition(&output, rt, D3D12_RESOURCE_STATE_COPY_SOURCE),
                crate::host::transition(
                    &compositor.prompts,
                    D3D12_RESOURCE_STATE_UNORDERED_ACCESS,
                    D3D12_RESOURCE_STATE_COPY_SOURCE,
                ),
            ]);
            cmds.CopyBufferRegion(&prompt_readback, 0, &compositor.prompts, 0, PROMPT_BYTES);
            cmds.ResourceBarrier(&[crate::host::transition(
                &compositor.prompts,
                D3D12_RESOURCE_STATE_COPY_SOURCE,
                D3D12_RESOURCE_STATE_UNORDERED_ACCESS,
            )]);
            (image_readback, footprint) =
                crate::capture::read_back(&host.device, cmds, &output).unwrap();
        }
        list.close();
        host.execute(&[&list]);
        unsafe {
            host.device.GetDeviceRemovedReason().unwrap();
        }
        let mut ptr = std::ptr::null_mut();
        unsafe {
            prompt_readback.Map(0, None, Some(&mut ptr)).unwrap();
        }
        let words =
            unsafe { std::slice::from_raw_parts(ptr.cast::<u32>(), PROMPT_BYTES as usize / 4) };
        let n = words[260].min(8) as usize;
        let hits: Vec<[u32; 6]> = (0..n)
            .map(|i| words[261 + 6 * i..267 + 6 * i].try_into().unwrap())
            .collect();
        unsafe {
            prompt_readback.Unmap(0, None);
        }
        eprintln!("GPU prompt hits: {hits:?}");
        let mut ptr = std::ptr::null_mut();
        unsafe {
            image_readback.Map(0, None, Some(&mut ptr)).unwrap();
        }
        let mut image = vec![0u8; 1280 * 700 * 3];
        for y in 0..700usize {
            for x in 0..1280usize {
                let p = unsafe {
                    ptr.cast::<u8>().add(
                        footprint.Offset as usize
                            + y * footprint.Footprint.RowPitch as usize
                            + x * 4,
                    )
                };
                image[(y * 1280 + x) * 3..(y * 1280 + x) * 3 + 3]
                    .copy_from_slice(unsafe { std::slice::from_raw_parts(p, 3) });
            }
        }
        unsafe {
            image_readback.Unmap(0, None);
        }
        (hits, image)
    }

    #[test]
    fn grey_lt_qte_and_subtitle_negative_on_gpu() {
        let Some(host) = crate::host::host() else {
            return;
        };
        let mut compositor = Composer::new(host, DXGI_FORMAT_R8G8B8A8_UNORM).unwrap();
        let mut pixels = vec![0u8; 1280 * 700 * 4];
        stamp(&mut pixels, 1227, 654, LT);
        stamp(&mut pixels, 953, 447, QTE);
        stamp(&mut pixels, 540, 650, SUBTITLE);
        let (hits, image) = render_fixture(host, &mut compositor, &pixels, None);
        let near = |hit: &[u32; 6], x: i64, y: i64| {
            (i64::from(hit[0]) - x).abs() <= 5 && (i64::from(hit[1]) - y).abs() <= 5
        };
        assert!(
            hits.iter().any(|h| near(h, 1244, 672) && h[4] == 0),
            "LT not detected: {hits:?}"
        );
        assert!(
            hits.iter()
                .any(|h| near(h, 987, 481) && h[4] == 6 && h[5] == 4),
            "QTE not detected: {hits:?}"
        );
        assert!(
            hits.iter().any(|h| near(h, 592, 665) && h[4] == 6),
            "subtitle X not detected: {hits:?}"
        );
        assert!(
            !hits.iter().any(|h| h[0] < 581 && h[0] > 530 && h[1] > 645),
            "ordinary subtitle text became an icon: {hits:?}"
        );
        for y in 447usize..516 {
            for x in 953usize..1022 {
                let r2 = (x as i32 - 987).pow(2) + (y as i32 - 481).pow(2);
                if !(12 * 12..=33 * 33).contains(&r2) {
                    continue;
                }
                let actual = &image[(y * 1280 + x) * 3..(y * 1280 + x) * 3 + 3];
                let expected = &pixels[(y * 1280 + x) * 4..(y * 1280 + x) * 4 + 3];
                assert!(
                    actual
                        .iter()
                        .zip(expected)
                        .all(|(a, b)| a.abs_diff(*b) <= 1),
                    "QTE ring changed at {x},{y}"
                );
            }
        }
    }

    fn stamp_objective(pixels: &mut [u8], cx: usize, cy: usize) {
        stamp(pixels, cx - 10, cy - 10, OBJECTIVE_Y);
        let dot_x = cx + 26;
        for y in cy - 5..=cy + 5 {
            for x in dot_x - 5..=dot_x + 5 {
                if (x as i32 - dot_x as i32).pow(2) + (y as i32 - cy as i32).pow(2) > 25 {
                    continue;
                }
                pixels[(y * 1280 + x) * 4..(y * 1280 + x) * 4 + 4]
                    .copy_from_slice(&[247, 63, 164, 255]);
            }
        }
    }

    fn stamp_compact_qte(pixels: &mut [u8], cx: usize, cy: usize, phase: usize) {
        // Same measured X face as 060, with the smaller animated annulus of 056.
        for y in 24..45usize {
            for x in 24..45usize {
                if (x as i32 - 34).pow(2) + (y as i32 - 34).pow(2) > 90 {
                    continue;
                }
                let c = QTE[y].as_bytes()[x];
                if c == b'.' {
                    continue;
                }
                let v = ((u16::from(c - b'0') * 255 + 4) / 9) as u8;
                let at = (((cy + y - 34) * 1280) + (cx + x - 34)) * 4;
                pixels[at..at + 4].copy_from_slice(&[v, v, v, 255]);
            }
        }
        for dy in -15i32..=15 {
            for dx in -15i32..=15 {
                let r = ((dx * dx + dy * dy) as f32).sqrt();
                if !(12.0..=14.0).contains(&r) {
                    continue;
                }
                let value = if ((dx + dy + phase as i32 * 5) % 19).abs() < 7 {
                    245
                } else {
                    125
                };
                let at = (((cy as i32 + dy) as usize * 1280) + (cx as i32 + dx) as usize) * 4;
                pixels[at..at + 4].copy_from_slice(&[value, value, value, 255]);
            }
        }
    }

    #[test]
    fn moving_compact_qte_and_objective_without_mouse_ghosts_on_gpu() {
        let Some(host) = crate::host::host() else {
            return;
        };
        let mut compositor = Composer::new(host, DXGI_FORMAT_R8G8B8A8_UNORM).unwrap();
        let blank = vec![0u8; 1280 * 700 * 4];
        render_fixture(host, &mut compositor, &blank, None);
        // Simulate mouse history already accumulated by the previous detector.
        let mut history = [0u32; 512];
        history[320] = 2;
        history[321..329].copy_from_slice(&[926, 450, 9, 9, 0, 2, 10, 0]);
        history[329..337].copy_from_slice(&[940, 456, 9, 9, 1, 2, 10, 0]);
        for (frame, (qx, qy, ox, oy)) in [
            (940, 450, 580, 395),
            (952, 438, 202, 618),
            (971, 428, 206, 622),
            (975, 430, 211, 625),
        ]
        .into_iter()
        .enumerate()
        {
            let mut pixels = vec![0u8; 1280 * 700 * 4];
            stamp_compact_qte(&mut pixels, qx, qy, frame);
            stamp_objective(&mut pixels, ox, oy);
            stamp(&mut pixels, 1227, 654, LT); // 049 EMPLOYE LT remains a real mouse.
            stamp(&mut pixels, 1192, 613, SUBTITLE); // 049 MENACER X stays SPACE.
            stamp(&mut pixels, 540, 650, SUBTITLE); // 052 ordinary subtitle text stays text.
            let seed = if frame == 0 { Some(&history) } else { None };
            let (hits, image) = render_fixture(host, &mut compositor, &pixels, seed);
            let near = |h: &[u32; 6], x: usize, y: usize| {
                h[0].abs_diff(x as u32) <= 3 && h[1].abs_diff(y as u32) <= 3
            };
            assert!(
                hits.iter()
                    .any(|h| near(h, qx, qy) && h[4] == 6 && h[5] == 4),
                "QTE frame {frame}: {hits:?}"
            );
            assert!(
                hits.iter().any(|h| near(h, ox, oy) && h[4] == 7),
                "Y frame {frame}: {hits:?}"
            );
            assert!(
                hits.iter().any(|h| near(h, 1244, 672) && h[4] == 0),
                "EMPLOYE frame {frame}: {hits:?}"
            );
            assert!(
                hits.iter().any(|h| near(h, 1244, 628) && h[4] == 6),
                "MENACER frame {frame}: {hits:?}"
            );
            assert!(
                !hits.iter().any(|h| h[0].abs_diff(qx as u32) < 34
                    && h[1].abs_diff(qy as u32) < 34
                    && h[4] != 6),
                "QTE mouse ghost: {hits:?}"
            );
            assert!(
                !hits.iter().any(|h| h[0] < 581 && h[0] > 530 && h[1] > 645),
                "Text false positive: {hits:?}"
            );
            assert_eq!(
                hits.iter().filter(|h| h[4] == 7).count(),
                1,
                "Stale moving Y: {hits:?}"
            );
            for dy in -16i32..=16 {
                for dx in -16i32..=16 {
                    if dx * dx + dy * dy < 11 * 11 {
                        continue;
                    }
                    let at = (qy as i32 + dy) as usize * 1280 + (qx as i32 + dx) as usize;
                    assert!(
                        image[at * 3..at * 3 + 3]
                            .iter()
                            .zip(&pixels[at * 4..at * 4 + 3])
                            .all(|(a, b)| a.abs_diff(*b) <= 1),
                        "QTE ring modified frame{frame}: {dx},{dy}"
                    );
                }
            }
        }
    }
}
