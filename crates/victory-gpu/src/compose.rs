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

/// Embedded 640x64 R8 alpha atlas (10 crisp 64x64 GTA VI-style white PC prompt icons):
/// 0: RMB (LT), 1: LMB (RT), 2: TAB (LB), 3: G (RB), 4: MAJ (A),
/// 5: R (B), 6: SPACE (X), 7: F (Y), 8: E (D-Pad Right), 9: C (RS).
const ATLAS_W: u32 = 640;
const ATLAS_H: u32 = 64;
const ATLAS_BYTES: &[u8] = include_bytes!("pc_prompts_atlas.bin");
const _: () = assert!(ATLAS_BYTES.len() == (ATLAS_W * ATLAS_H) as usize);

/// Updated by `victory_hle::modules::gameinput`: when a real XInput gamepad is
/// connected, keep the original Xbox button icons automatically.
static REAL_PAD_CONNECTED: AtomicBool = AtomicBool::new(false);

pub fn set_real_pad_connected(connected: bool) {
    REAL_PAD_CONNECTED.store(connected, Ordering::Relaxed);
}

const SOURCE: &str = r#"
#define RS "RootConstants(num32BitConstants=5, b0), DescriptorTable(SRV(t0, numDescriptors=3), UAV(u0, numDescriptors=2)), StaticSampler(s0, filter=FILTER_MIN_MAG_MIP_LINEAR, addressU=TEXTURE_ADDRESS_CLAMP, addressV=TEXTURE_ADDRESS_CLAMP)"

cbuffer Params : register(b0) {
    uint w;
    uint h;
    uint premultiplied;
    float sharpen;
    uint pc_prompts;
};

Texture2D<float4> game : register(t0);
Texture2D<float4> ui : register(t1);
Texture2D<float> atlas : register(t2);
RWTexture2D<float4> outp : register(u0);

// Prompt detection state (raw u32 buffer):
// dword 0: raw candidate count (0..64)
// dwords 1..1 + 64*4: raw candidates (cx, cy, icon_id, score_u32)
// dword 260: merged hit count (0..8)
// dwords 261..261 + 8*6: merged hits (cx, cy, hw, hh, icon_id, packed_bg_rgba8)
RWByteAddressBuffer prompts : register(u1);

SamplerState linear_clamp : register(s0);

static const float3 LUMA = float3(0.299, 0.587, 0.114);

uint pack_rgba8(float4 c) {
    uint4 q = uint4(saturate(c) * 255.0 + 0.5);
    return q.r | (q.g << 8) | (q.b << 16) | (q.a << 24);
}

float4 unpack_rgba8(uint v) {
    return float4(v & 255u, (v >> 8) & 255u, (v >> 16) & 255u, (v >> 24) & 255u) / 255.0;
}

float4 sample_ui_px(float2 px) {
    float2 uv = (px + 0.5) / float2(w, h);
    return ui.SampleLevel(linear_clamp, uv, 0);
}

bool is_popup_bg(float4 c) {
    float l = dot(c.rgb, LUMA);
    return c.a > 0.65 && l < 0.26;
}

bool is_white_glyph(float4 c) {
    float l = dot(c.rgb, LUMA);
    float hi = max(c.r, max(c.g, c.b));
    float lo = min(c.r, min(c.g, c.b));
    return c.a > 0.80 && l > 0.70 && (hi - lo) < 0.22;
}

// 4: A (green), 5: B (red), 6: X (blue), 7: Y (yellow), -1: none
int classify_abxy(float4 c) {
    if (c.a < 0.75) return -1;
    if (c.g > 0.42 && (c.g - c.r) > 0.15 && (c.g - c.b) > 0.15) return 4;
    if (c.r > 0.52 && (c.r - c.g) > 0.20 && (c.r - c.b) > 0.20) return 5;
    if (c.b > 0.48 && (c.b - c.r) > 0.18 && c.g > 0.18) return 6;
    if (c.r > 0.62 && c.g > 0.50 && c.b < 0.30 && (c.r - c.b) > 0.38) return 7;
    return -1;
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
// Runs on a 4px grid (w/4 x h/4): detects Xbox button prompts inside dark HUD pop-up boxes.
[RootSignature(RS)]
[numthreads(8, 8, 1)]
void detect_hits(uint3 tid : SV_DispatchThreadID) {
    float scale = float(h) / 1080.0;
    float2 p = float2(tid.xy * 4u + 2u);
    if (p.x < 24.0 * scale || p.x >= float(w) - 24.0 * scale ||
        p.y < 24.0 * scale || p.y >= float(h) - 24.0 * scale) {
        return;
    }
    // Exclude bottom-left minimap/radar region to guarantee zero false positives.
    if (p.x < float(w) * 0.24 && p.y > float(h) * 0.68) {
        return;
    }

    float4 center = sample_ui_px(p);
    if (center.a < 0.65) return;

    float pad = 16.0 * scale;
    float4 top_bg = sample_ui_px(p + float2(0.0, -pad));
    float4 bot_bg = sample_ui_px(p + float2(0.0,  pad));
    float4 l_bg   = sample_ui_px(p + float2(-pad, 0.0));
    float4 r_bg   = sample_ui_px(p + float2( pad, 0.0));
    if (!is_popup_bg(top_bg) || !is_popup_bg(bot_bg)) return;
    if (!is_popup_bg(l_bg) || !is_popup_bg(r_bg)) return;

    // 1) Colored A/B/X/Y circle buttons inside pop-up boxes.
    int col_icon = classify_abxy(center);
    if (col_icon >= 0) {
        float match_cnt = 0.0;
        float half_box = 10.5 * scale;
        for (int j = 0; j < 6; j++) {
            for (int i = 0; i < 6; i++) {
                float2 off = float2(-half_box + float(i) * (2.0 * half_box / 5.0),
                                    -half_box + float(j) * (2.0 * half_box / 5.0));
                if (classify_abxy(sample_ui_px(p + off)) == col_icon) {
                    match_cnt += 1.0;
                }
            }
        }
        float ratio = match_cnt / 36.0;
        if (ratio >= 0.36) {
            uint slot;
            prompts.InterlockedAdd(0, 1, slot);
            if (slot < 64u) {
                uint base = (1u + slot * 4u) * 4u;
                prompts.Store4(base, uint4(uint(p.x), uint(p.y), uint(col_icon), uint(ratio * 1000.0)));
            }
        }
        return;
    }

    // 2) Solid white trigger/bumper silhouettes (LT, RT, LB, RB) with dark inner letters.
    // Measure an 8x8 white-density grid across a ~23px box (scaled for output height).
    float half_w = 11.5 * scale;
    float half_h = 11.5 * scale;
    float total = 0.0;
    float top_bar = 0.0;
    float bot_bar = 0.0;
    float tl = 0.0, tr = 0.0, bl = 0.0, br = 0.0;

    for (int j = 0; j < 8; j++) {
        for (int i = 0; i < 8; i++) {
            float2 off = float2(-half_w + float(i) * (2.0 * half_w / 7.0),
                                -half_h + float(j) * (2.0 * half_h / 7.0));
            float v = is_white_glyph(sample_ui_px(p + off)) ? 1.0 : 0.0;
            total += v;
            if (j < 2 && i >= 1 && i <= 6) top_bar += v;
            if (j >= 6 && i >= 1 && i <= 6) bot_bar += v;
            if (j < 2 && i < 2) tl += v;
            if (j < 2 && i >= 6) tr += v;
            if (j >= 6 && i < 2) bl += v;
            if (j >= 6 && i >= 6) br += v;
        }
    }

    float fill = total / 64.0;
    if (fill < 0.46) return;
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
        uint slot;
        prompts.InterlockedAdd(0, 1, slot);
        if (slot < 64u) {
            uint base = (1u + slot * 4u) * 4u;
            prompts.Store4(base, uint4(uint(p.x), uint(p.y), uint(icon), uint(fill * 1000.0)));
        }
    }
}
#endif

#ifdef MERGE_HITS
[RootSignature(RS)]
[numthreads(1, 1, 1)]
void merge_hits() {
    uint raw_n = min(prompts.Load(0), 64u);
    if (raw_n == 0u) {
        prompts.Store(260 * 4, 0u);
        return;
    }
    float scale = float(h) / 1080.0;
    float merge_r = 22.0 * scale;

    bool used[64];
    [unroll]
    for (uint k = 0u; k < 64u; k++) used[k] = false;

    uint out_n = 0u;
    for (uint i = 0u; i < raw_n && out_n < 8u; i++) {
        if (used[i]) continue;
        uint4 a = prompts.Load4((1u + i * 4u) * 4u);
        float sw = float(a.w);
        float sx = float(a.x) * sw;
        float sy = float(a.y) * sw;
        used[i] = true;
        for (uint j = i + 1u; j < raw_n; j++) {
            if (used[j]) continue;
            uint4 b = prompts.Load4((1u + j * 4u) * 4u);
            if (b.z != a.z) continue;
            float2 d = float2(float(b.x) - float(a.x), float(b.y) - float(a.y));
            if (dot(d, d) <= merge_r * merge_r) {
                float bw = float(b.w);
                sx += float(b.x) * bw;
                sy += float(b.y) * bw;
                sw += bw;
                used[j] = true;
            }
        }
        float cx = sx / max(sw, 1.0);
        float cy = sy / max(sw, 1.0);
        float4 bg = sample_ui_px(float2(cx, cy - 16.0 * scale));
        uint hw = uint(15.0 * scale + 0.5);
        uint hh = uint(15.0 * scale + 0.5);
        uint dst = (261u + out_n * 6u) * 4u;
        prompts.Store4(dst, uint4(uint(cx + 0.5), uint(cy + 0.5), hw, hh));
        prompts.Store2(dst + 16u, uint2(a.z, pack_rgba8(bg)));
        out_n++;
    }
    prompts.Store(260 * 4, out_n);
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
    // Local contrast weight avoids haloing on sharp edges while crisping subtle detail.
    float3 amp = saturate(min(mn, 1.0 - mx) / max(mx, 1e-4));
    float3 weight = sqrt(amp) * (-0.16 * sharpen);
    float3 res = (c + (n + s + e + b) * weight) / (1.0 + 4.0 * weight);
    return saturate(res);
}

[RootSignature(RS)]
[numthreads(8, 8, 1)]
void main(uint3 id : SV_DispatchThreadID) {
    if (id.x >= w || id.y >= h) return;
    float2 uv = (float2(id.xy) + 0.5) / float2(w, h);
    float3 g = sharpen_game(int2(id.xy));
    float4 u = ui.SampleLevel(linear_clamp, uv, 0);

    if (pc_prompts != 0u) {
        uint n = min(prompts.Load(260 * 4), 8u);
        for (uint k = 0u; k < n; k++) {
            uint src = (261u + k * 6u) * 4u;
            uint4 box = prompts.Load4(src);
            uint2 meta = prompts.Load2(src + 16u);
            float2 cpos = float2(box.xy);
            float2 half_sz = float2(box.zw);
            float2 rel = (float2(id.xy) - cpos) / max(half_sz, float2(1.0, 1.0));
            if (abs(rel.x) <= 1.0 && abs(rel.y) <= 1.0) {
                float2 tile_uv = rel * 0.5 + 0.5;
                float u_atlas = (float(meta.x) + tile_uv.x) / 10.0;
                float glyph = atlas.SampleLevel(linear_clamp, float2(u_atlas, tile_uv.y), 0);
                float4 bg = unpack_rgba8(meta.y);
                float3 fg = float3(0.96, 0.96, 0.96);
                float3 replaced = lerp(bg.rgb, fg, glyph);
                float alpha = max(bg.a, glyph);
                u = float4(premultiplied != 0u ? replaced * alpha : replaced, alpha);
                break;
            }
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

        // 640x64 R8_UNORM texture for PC prompt icons.
        let atlas_desc = D3D12_RESOURCE_DESC {
            Dimension: D3D12_RESOURCE_DIMENSION_TEXTURE2D,
            Width: u64::from(ATLAS_W),
            Height: ATLAS_H,
            DepthOrArraySize: 1,
            MipLevels: 1,
            Format: DXGI_FORMAT_R8_UNORM,
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

        // Row pitch must be 256-byte aligned: 640 -> 768 bytes per row.
        let row_pitch = (ATLAS_W + 255) & !255;
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
                ATLAS_BYTES.as_ptr().add(y * ATLAS_W as usize),
                dst.add(y * row_pitch as usize),
                ATLAS_W as usize,
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

    // One-time GPU upload of the 640x64 R8 PC prompts atlas on the first frame.
    if let Some(upload) = pass.atlas_upload.take() {
        let row_pitch = (ATLAS_W + 255) & !255;
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
                        Format: DXGI_FORMAT_R8_UNORM,
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
            // Keep upload resource alive past this first command list execution (only 48 KiB).
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
        Format: DXGI_FORMAT_R8_UNORM,
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
    let constants: [u32; 5] = [
        w as u32,
        h,
        u32::from(premultiplied()),
        sharpen_amount().to_bits(),
        u32::from(do_prompts),
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
        list.SetComputeRoot32BitConstants(0, 5, constants.as_ptr().cast(), 0);
        list.SetComputeRootDescriptorTable(1, gpu);

        if do_prompts {
            list.SetPipelineState(&pass.pso_clear);
            list.Dispatch(1, 1, 1);
            list.ResourceBarrier(&uav_barrier);

            list.SetPipelineState(&pass.pso_detect);
            list.Dispatch((w as u32).div_ceil(32), h.div_ceil(32), 1);
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

