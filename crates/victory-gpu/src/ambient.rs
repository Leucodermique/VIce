//! A stand-in for the indirect light the title traces rays for.
//!
//! With the ray-tracing structure builder off (`crate::pipeline::builder_runs`)
//! the title's rays find nothing. Its program that resolves the diffuse light
//! of its screen probes (`CS_ScreenProbes_TemporalResolve_*`) then leaves the
//! sky's light outdoors, nothing indoors (walls are black where no lamp or
//! sun reaches) and, in blocks of the picture's tiles, what the probes'
//! memory held. Right after that program the light is raised to an estimate
//! and bounded by a multiple of it.
//!
//! That a stand-in can be put there, and in which of the program's images,
//! is beloooo's find (VIctory-Belobug-AMD, 06031237, 4f45ab88, cd9f78ce,
//! GPL-3.0-or-later), which puts a constant there, shaded in creases from the
//! scene's depth. The estimate here is worked out from the frame instead, so
//! it follows the hour, the room and its lamps by itself: the light the
//! visible surfaces send out (the title keeps it for its own screen traces:
//! `CS_CacheScreenSpaceRadiance`, the frame before) is averaged in 64x36
//! cells, and every point gets the mean of the cells weighted by the share of
//! its surroundings they cover, as a directional value (first-order harmonics
//! at 160x90 points, taken between them by depth).
//!
//! What it assumes: what is not on screen sends out what is (with a slow
//! mean of the last seconds, so that turning into a dark corner is not black
//! at once). No occlusion but the creases'.
//!
//! `VICTORY_AMBIENT`:
//! - unset or `0`/`off`: the title's own output.
//! - a number: that share of the estimate (1: all of it).
//! - `constant:<level>`: the constant, as beloooo's fork has it.
//!
//! `VICTORY_AMBIENT_SHADE=<strength>,<radius in metres>` sets the creases.
//!
//! The air of a room is lit by the same probes: without rays they hold the
//! sky's light indoors too, a brown veil at sunset. `VICTORY_AMBIENT_AIR`
//! bounds what the air scatters by a multiple of the light around.
//!
//! The reflections are traced too, and resolved by programs at the picture's
//! size (R9G9B9E5 as 32-bit integers). With `VICTORY_AMBIENT_REFLECT=<multiple>`
//! and `VICTORY_AMBIENT_REFLECT_IN=<text>[,<text>]` (e.g.
//! `CS_TemporalResolve2_Opaque,CS_TemporalResolve1_Alpha`), right after such a
//! program each texel is held to no more than that multiple of the light around
//! it, hue kept, preventing 100-1000x neon/bloom explosions on reflective
//! indoor surfaces (bank counters, glass, wet floors) when RT is off.
//! Ported from Serph91P's avatarmanz/VIctory-7900XTX#314 & Vice-Tay #127.

use std::sync::{Mutex, OnceLock};

use windows::Win32::Graphics::Direct3D12::*;
use windows::core::s;

use crate::host::{HostList, host, transition};

/// Programs of the title the stand-in follows.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub(crate) enum Role {
    /// Resolves the diffuse light.
    Resolve,
    /// Keeps the light visible surfaces send out.
    Radiance,
    /// Adds the lamps' light to what the air scatters (the last program
    /// that writes that volume before it is summed up along the view).
    Air,
    /// Resolves the traced reflections.
    Reflect,
}

/// The program's part in this, by its name in the container.
pub(crate) fn role(container: &[u8]) -> Option<Role> {
    role_in(container, reflect_in())
}

/// [`role`], with the texts that name the reflections' resolves (looked
/// for last, so that no text takes a program of the other roles).
fn role_in(container: &[u8], reflect_in: &[String]) -> Option<Role> {
    let has = |name: &[u8]| container.windows(name.len()).any(|w| w == name);
    if has(b"CS_ScreenProbes_TemporalResolve") {
        Some(Role::Resolve)
    } else if has(b"CS_CacheScreenSpaceRadiance") {
        Some(Role::Radiance)
    } else if has(b"CS_AccumulateLocalLightInscatteringVolume") {
        Some(Role::Air)
    } else if reflect_in.iter().any(|name| has(name.as_bytes())) {
        Some(Role::Reflect)
    } else {
        None
    }
}

/// Where the resolve writes the diffuse light (R9G9B9E5 as 32-bit integers)
/// and reads the scene's depth, and where the other writes the light of
/// visible surfaces: root parameter and place in its table, in dwords.
pub(crate) const LIGHT: (u8, u32) = (1, 176);
pub(crate) const DEPTH: (u8, u32) = (0, 464);
pub(crate) const RADIANCE: (u8, u32) = (1, 16);
/// Where the light the air scatters is.
pub(crate) const AIR: (u8, u32) = (1, 32);
/// Where the reflections' resolves write them: the first of these places
/// that holds a 2D image of their format (the opaque surfaces' resolve has it
/// at the first, the transparent ones' at the second; a buffer at the first).
pub(crate) const REFLECTIONS: [(u8, u32); 2] = [(1, 128), (1, 136)];

#[derive(Clone, Copy, PartialEq, Debug)]
enum Set {
    Off,
    Estimate(f32),
    Constant(f32),
}

fn parse(set: &str) -> Set {
    let set = set.trim();
    let number = |t: &str| t.trim().parse::<f32>().ok().filter(|v| v.is_finite() && *v > 0.0);
    match set.strip_prefix("constant:") {
        Some(level) => number(level).map_or(Set::Off, Set::Constant),
        None => number(set).map_or(Set::Off, Set::Estimate),
    }
}

fn set() -> Set {
    static SET: OnceLock<Set> = OnceLock::new();
    *SET.get_or_init(|| parse(&std::env::var("VICTORY_AMBIENT").unwrap_or_default()))
}

/// Whether the stand-in is on: set, and the title's rays find nothing.
pub(crate) fn on() -> bool {
    set() != Set::Off && !crate::pipeline::builder_runs()
}

/// The most the air scatters, as a multiple of the light around
/// (`VICTORY_AMBIENT_AIR=<multiple>`; unset or 0: what the title has).
fn air() -> f32 {
    static SET: OnceLock<f32> = OnceLock::new();
    *SET.get_or_init(|| {
        let set = std::env::var("VICTORY_AMBIENT_AIR").unwrap_or_default();
        set.trim().parse().ok().filter(|v: &f32| v.is_finite() && *v > 0.0).unwrap_or(0.0)
    })
}

/// The most a reflection may hold, as a multiple of the light around
/// (`VICTORY_AMBIENT_REFLECT=<multiple>`; unset, `0` or `off`: the title's
/// own). Acts only while `VICTORY_AMBIENT` is set and the builder does not run.
fn reflect() -> f32 {
    static SET: OnceLock<f32> = OnceLock::new();
    *SET.get_or_init(|| parse_reflect(&std::env::var("VICTORY_AMBIENT_REFLECT").unwrap_or_default()))
}

fn parse_reflect(set: &str) -> f32 {
    set.trim()
        .parse()
        .ok()
        .filter(|v: &f32| v.is_finite() && *v > 0.0)
        .unwrap_or(0.0)
}

/// Texts in the names of the reflections' resolve programs
/// (`VICTORY_AMBIENT_REFLECT_IN=<text>[,<text>]`); empty when the hold is off.
fn reflect_in() -> &'static [String] {
    static SET: OnceLock<Vec<String>> = OnceLock::new();
    SET.get_or_init(|| {
        if reflect() == 0.0 {
            return Vec::new();
        }
        let names = parse_reflect_in(&std::env::var("VICTORY_AMBIENT_REFLECT_IN").unwrap_or_default());
        if !names.is_empty() {
            tracing::info!(held = reflect(), shaders = ?names, "ambient reflection hold configured");
        }
        names
    })
}

fn parse_reflect_in(set: &str) -> Vec<String> {
    set.split(',')
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(str::to_owned)
        .collect()
}

/// LOCAL: how many times the estimate the title's own result may keep
/// (`VICTORY_AMBIENT_MOST=<multiple>`, 4 when unset). Without rays the
/// probes hold the sky's light indoors too; at 4 times the estimate a room
/// looked towards a window or the sky goes very bright.
fn most() -> f32 {
    static SET: OnceLock<f32> = OnceLock::new();
    *SET.get_or_init(|| {
        let set = std::env::var("VICTORY_AMBIENT_MOST").unwrap_or_default();
        set.trim().parse().ok().filter(|v: &f32| v.is_finite() && *v > 0.0).unwrap_or(4.0)
    })
}

/// Strength and radius of the creases' shade.
fn shade() -> [f32; 2] {
    static SET: OnceLock<[f32; 2]> = OnceLock::new();
    *SET.get_or_init(|| {
        let mut shade = [1.0, 0.6];
        let set = std::env::var("VICTORY_AMBIENT_SHADE").unwrap_or_default();
        for (to, v) in shade.iter_mut().zip(set.split(',')) {
            if let Ok(v) = v.trim().parse() {
                *to = v;
            }
        }
        shade
    })
}

const SOURCE: &str = r#"
#ifdef HOLD_INDIRECT
#define RS "RootConstants(num32BitConstants=13, b0), DescriptorTable(SRV(t0)), DescriptorTable(UAV(u0)), UAV(u1), UAV(u2), SRV(t1)"
#else
#define RS "RootConstants(num32BitConstants=13, b0), DescriptorTable(SRV(t0)), DescriptorTable(UAV(u0)), UAV(u1), UAV(u2)"
#endif
#define CX 64
#define CY 36
#define CELLS (CX * CY)
#define PX 160
#define PY 90
cbuffer Set : register(b0) {
    float level; float constant; float strength; float radius;
    float near; float tan_y; uint width; uint height;
    float follow; float most; float air; float unused;
    uint indirect_at;
};
Texture2D<float> depth : register(t0);
// Cells: the light they send out (then, at CELLS, the slow mean), from
// CELLS + 1 on where they are.
RWStructuredBuffer<float4> cells : register(u1);
// Three a point: red, green and blue, each a mean and a direction.
RWStructuredBuffer<float4> points : register(u2);
static const float3 LUMA = float3(0.2126, 0.7152, 0.0722);

// The point seen at a texel, in metres from the eye (x right, y up, z away).
float3 seen(int2 p, float2 size) {
    float d = depth.Load(int3(clamp(p, int2(0, 0), int2(size) - 1), 0));
    float z = min(near / max(d, 1e-7), 1000.0);
    float2 ndc = (float2(p) + 0.5) / size * 2.0 - 1.0;
    return float3(ndc.x * tan_y * size.x / size.y * z, -ndc.y * tan_y * z, z);
}

#ifdef KEEP
RWTexture2D<float3> radiance : register(u0);
[RootSignature(RS)]
[numthreads(8, 4, 1)]
void keep(uint3 at : SV_DispatchThreadID) {
    uint w, h;
    radiance.GetDimensions(w, h);
    float3 sum = 0;
    for (uint y = 0; y < 5; y++) for (uint x = 0; x < 5; x++) {
        uint2 p = uint2((float2(at.xy) + (float2(x, y) + 0.5) / 5.0) * float2(w, h) / float2(CX, CY));
        float3 c = max(radiance[p], 0.0);
        c = any(isnan(c)) || any(isinf(c)) ? 0.0 : c;
        // Lamps and glints count, but clamp spikes tightly so a single flash
        // never poisons the slow room mean for 5-6 seconds.
        sum += c / max(1.0, dot(c, LUMA) / 4.0);
    }
    cells[at.y * CX + at.x] = float4(sum / 25.0, 1.0);
}
[RootSignature(RS)]
[numthreads(1, 1, 1)]
void mean() {
    float3 sum = 0;
    for (uint i = 0; i < CELLS; i++) sum += cells[i].rgb;
    float4 was = cells[CELLS];
    float3 now = sum / CELLS;
    if (any(isnan(now)) || any(isinf(now))) now = 0.0;
    float now_l = dot(now, LUMA);
    if (now_l > 3.5) now *= 3.5 / now_l;
    if (was.a > 0.0 && !any(isnan(was.rgb)) && !any(isinf(was.rgb))) {
        // Rise gently and reject sudden full-screen flash spikes; recover 6x faster when dimming.
        float3 capped = min(now, was.rgb * 1.4 + 0.25);
        float rate = dot(capped, LUMA) < dot(was.rgb, LUMA) ? max(follow * 6.0, 0.25) : follow;
        now = lerp(was.rgb, capped, rate);
    }
    cells[CELLS] = float4(now, 1.0);
}
#endif

#ifdef PLACE
[RootSignature(RS)]
[numthreads(8, 4, 1)]
void place(uint3 at : SV_DispatchThreadID) {
    float2 size = float2(width, height);
    float3 p = seen(int2((float2(at.xy) + 0.5) * size / float2(CX, CY)), size);
    cells[CELLS + 1 + at.y * CX + at.x] = float4(p, p.z * p.z);
}
#endif

#ifdef GATHER
[RootSignature(RS)]
[numthreads(8, 2, 1)]
void gather(uint3 at : SV_DispatchThreadID) {
    float2 size = float2(width, height);
    float3 p = seen(int2((float2(at.xy) + 0.5) * size / float2(PX, PY)), size);
    float r2 = 0.25 + 0.0025 * p.z * p.z;
    float whole = 0;
    float3 toward = 0, light = 0, red = 0, green = 0, blue = 0;
    for (uint i = 0; i < CELLS; i++) {
        float4 c = cells[CELLS + 1 + i];
        float3 L = cells[i].rgb;
        float3 d = c.xyz - p;
        float dd = dot(d, d);
        float w = c.w / (dd + r2);
        float3 dir = d / sqrt(dd + 0.25 * r2);
        whole += w;
        toward += w * dir;
        light += w * L;
        red += w * L.r * dir;
        green += w * L.g * dir;
        blue += w * L.b * dir;
    }
    whole = max(whole, 1e-6);
    light /= whole;
    toward /= whole;
    uint at0 = (at.y * PX + at.x) * 3;
    points[at0] = float4(red / whole - light.r * toward, light.r);
    points[at0 + 1] = float4(green / whole - light.g * toward, light.g);
    points[at0 + 2] = float4(blue / whole - light.b * toward, light.b);
}
#endif

#ifdef AIR
RWTexture3D<float3> scattered : register(u0);
// No more than a multiple of the light around: of the room as the last
// seconds showed it, and of the surface seen in this direction (towards a
// window the air outside may be as bright as the sky is).
[RootSignature(RS)]
[numthreads(8, 8, 2)]
void clear_air(uint3 at : SV_DispatchThreadID) {
    uint w, h, d;
    scattered.GetDimensions(w, h, d);
    if (at.x >= w || at.y >= h || at.z >= d) return;
    uint2 q = min(uint2((float2(at.xy) + 0.5) / float2(w, h) * float2(PX, PY)), uint2(PX - 1, PY - 1));
    uint at0 = (q.y * PX + q.x) * 3;
    float3 seen_there = float3(points[at0].w, points[at0 + 1].w, points[at0 + 2].w);
    float far = float(at.z) / float(d);
    // Volumetric inscattering is integrated across all depth slices: without RT,
    // sunset sky leaks into indoor froxels and turns the whole room into an opaque
    // peach/white wall for 5-6 seconds. Cap strictly per slice (tighter near camera).
    float slice_max = lerp(0.020, 0.045, smoothstep(0.15, 0.70, far));
    float cap = min(air * lerp(dot(cells[CELLS].rgb, LUMA), dot(seen_there, LUMA), far), slice_max);
    float3 c = scattered[at];
    // LOCAL: a NaN or infinity in the air is summed along every view ray
    // through it: black or white patches floating in the room.
    if (any(isnan(c)) || any(isinf(c))) { scattered[at] = 0; return; }
    float has = dot(c, LUMA);
    if (has > cap) scattered[at] = c * (cap / has);
}
#endif

#if defined(RAISE) || defined(HOLD) || defined(HOLD_INDIRECT)
RWTexture2D<uint> light : register(u0);
float3 unpack(uint v) {
    return float3(v & 511, (v >> 9) & 511, (v >> 18) & 511) * exp2(float(v >> 27) - 24.0);
}
uint pack(float3 c) {
    c = clamp(c, 0.0, 65408.0);
    float top = max(c.r, max(c.g, c.b));
    int e = clamp(int(floor(log2(max(top, 1e-9)))) + 16, 0, 31);
    float scale = exp2(float(24 - e));
    if (top * scale >= 511.5) { e = min(e + 1, 31); scale = exp2(float(24 - e)); }
    uint3 m = min(uint3(c * scale + 0.5), 511u);
    return m.r | (m.g << 9) | (m.b << 18) | (uint(e) << 27);
}
#endif

#if defined(HOLD) || defined(HOLD_INDIRECT)
#ifdef HOLD_INDIRECT
ByteAddressBuffer bounded : register(t1);
#endif
// The reflections: no brighter than `most` times the light around, as the
// estimate has it at the four points about the texel (with a fifth of the
// room's slow mean), or the constant. Also scrubs NaN/Inf and bounds extreme
// single-channel neon reflections (pink/green disco bloom on glass/counters).
[RootSignature(RS)]
[numthreads(8, 8, 1)]
void hold(uint3 at : SV_DispatchThreadID) {
#ifdef HOLD_INDIRECT
    if (any(bounded.Load3(indirect_at) == 0)) return;
#endif
    uint w, h;
    light.GetDimensions(w, h);
    if (at.x >= w || at.y >= h) return;
    float3 c = unpack(light[at.xy]);
    if (any(isnan(c)) || any(isinf(c))) {
        light[at.xy] = 0;
        return;
    }
    float3 around = float3(1.0, 0.98, 0.94) * constant;
    if (constant <= 0.0) {
        float2 g = (float2(at.xy) + 0.5) / float2(w, h) * float2(PX, PY) - 0.5;
        int2 g0 = int2(floor(g));
        float2 f = g - float2(g0);
        around = 0;
        for (int k = 0; k < 4; k++) {
            int2 q = clamp(g0 + int2(k & 1, k >> 1), int2(0, 0), int2(PX, PY) - 1);
            float2 b = float2(k & 1 ? f.x : 1 - f.x, k >> 1 ? f.y : 1 - f.y);
            uint at0 = (q.y * PX + q.x) * 3;
            around += b.x * b.y * float3(points[at0].w, points[at0 + 1].w, points[at0 + 2].w);
        }
        around = lerp(around, cells[CELLS].rgb, 0.2);
    }
    float top = clamp(most * dot(around, LUMA), 0.02, 3.0);
    // Bound both luminance and peak single-channel energy (magenta/neon green
    // have low luma relative to R+B or G, so clamping max channel prevents bloom explosions).
    float peak = max(c.r, max(c.g, c.b));
    float has = max(dot(c, LUMA), peak * 0.65);
    if (has > top) {
        light[at.xy] = pack(c * (top / has));
    }
}
#endif

#ifdef RAISE
[RootSignature(RS)]
[numthreads(8, 8, 1)]
void raise(uint3 at : SV_DispatchThreadID) {
    float2 size = float2(width, height);
    if (at.x >= width || at.y >= height) return;
    int2 p = int2(at.xy);
    float3 c = seen(p, size);
    float3 l = seen(p + int2(-1, 0), size), r = seen(p + int2(1, 0), size);
    float3 u = seen(p + int2(0, -1), size), d = seen(p + int2(0, 1), size);
    float3 dx = abs(l.z - c.z) < abs(r.z - c.z) ? c - l : r - c;
    float3 dy = abs(u.z - c.z) < abs(d.z - c.z) ? c - u : d - c;
    float3 n = normalize(cross(dy, dx) + float3(0, 0, -1e-9));
    if (dot(n, c) > 0.0) n = -n;
    // How open it is (as in beloooo's cd9f78ce).
    float open = 1.0;
    float px = min(radius * size.y / (2.0 * tan_y * c.z), 128.0);
    float noise = frac(52.9829189 * frac(dot(float2(p), float2(0.06711056, 0.00583715))));
    if (strength > 0.0 && px >= 1.5 && c.z < 400.0) {
        float sum = 0.0;
        const int N = 12;
        for (int i = 0; i < N; i++) {
            float a = i * 2.399963 + noise * 6.2831853;
            float2 o = float2(cos(a), sin(a)) * (sqrt((i + 0.5) / N) * px);
            float3 v = seen(p + int2(round(o)), size) - c;
            float vv = dot(v, v);
            float over = dot(v, n) - 0.004 * c.z - 0.005;
            sum += max(over, 0.0) / sqrt(vv + 1e-6) * saturate(1.0 - vv / (radius * radius));
        }
        open = saturate(1.0 - strength * 2.0 * sum / N);
    }
    float3 found;
    if (constant > 0.0) {
        found = float3(1.0, 0.98, 0.94) * constant * open;
        light[p] = pack(found);
        return;
    }
    // Between the four points around, those at its own depth first; which
    // four is dithered a little.
    float2 g = (float2(p) + 0.5) / size * float2(PX, PY) - 0.5 + (noise - 0.5) * 0.5;
    int2 g0 = int2(floor(g));
    float2 f = g - float2(g0);
    float4 sr = 0, sg = 0, sb = 0;
    float sum = 0;
    for (int k = 0; k < 4; k++) {
        int2 q = clamp(g0 + int2(k & 1, k >> 1), int2(0, 0), int2(PX, PY) - 1);
        float z = seen(int2((float2(q) + 0.5) * size / float2(PX, PY)), size).z;
        float2 b = float2(k & 1 ? f.x : 1 - f.x, k >> 1 ? f.y : 1 - f.y);
        float w = (b.x * b.y + 1e-3) / (0.02 + abs(z - c.z) / c.z);
        uint at0 = (q.y * PX + q.x) * 3;
        sr += w * points[at0];
        sg += w * points[at0 + 1];
        sb += w * points[at0 + 2];
        sum += w;
    }
    float3 all = float3(sr.w, sg.w, sb.w) / sum;
    found = all + 1.2 * float3(dot(sr.xyz, n), dot(sg.xyz, n), dot(sb.xyz, n)) / sum;
    found = clamp(found, 0.2 * all, 2.5 * all);
    float3 room_mean = cells[CELLS].rgb;
    found = lerp(found, room_mean, 0.28) * level * open;
    // Soft multi-bounce indoor floor: keeps dark back-rooms (e.g. cafe counter
    // where the waitress walks for water) readable instead of crushing into
    // pitch black when RTGI is off, while leaving sunlit faces and bright areas
    // completely untouched via `max`.
    float3 bounce_floor = max(room_mean * 0.55, float3(0.065, 0.060, 0.055)) * level * (0.55 + 0.45 * open);
    found = max(found, bounce_floor);
    if (any(isnan(found)) || any(isinf(found))) found = 0.0;
    float found_l = dot(found, LUMA);
    if (found_l > 3.5) found *= 3.5 / found_l;
    float3 was = unpack(light[p]);
    if (any(isnan(was)) || any(isinf(was))) was = 0.0;
    // Magenta/violet probe garbage filter (nvidia #29 / Belobug-AMD): kills purple-blue blotches on lit paper.
    if (was.g < 0.5 * min(was.r, was.b) && max(was.r, was.b) > 0.02) was = 0.0;
    if (was.b > 2.5 * was.g && was.b > was.r && was.b > 0.02) was = 0.0;
    // What the program left counts as far as it can be true: the sky's light
    // outdoors, not the blocks of other memory or full-screen HDR flashes.
    float top = clamp(dot(found, LUMA) * most, 1e-6, 5.0);
    float has = dot(was, LUMA);
    was *= min(1.0, top / max(has, 1e-6));
    float3 final_l = max(was, found);
    float fl = dot(final_l, LUMA);
    if (fl > 5.0) final_l *= 5.0 / fl;
    light[p] = pack(final_l);
}
#endif
"#;

/// Bytes of the cells' buffer and of the points'.
const CELLS: u64 = 64 * 36;
const CELL_BYTES: u64 = (2 * CELLS + 1) * 16;
const POINT_BYTES: u64 = 160 * 90 * 48;

struct Pass {
    root: ID3D12RootSignature,
    keep: ID3D12PipelineState,
    mean: ID3D12PipelineState,
    place: ID3D12PipelineState,
    gather: ID3D12PipelineState,
    raise: ID3D12PipelineState,
    air: ID3D12PipelineState,
    hold: Option<ID3D12PipelineState>,
    hold_indirect: Option<(ID3D12RootSignature, ID3D12PipelineState)>,
    cells: ID3D12Resource,
    points: ID3D12Resource,
}
// SAFETY: D3D12 objects are free-threaded.
unsafe impl Send for Pass {}
// SAFETY: as above.
unsafe impl Sync for Pass {}

fn make(
    device: &ID3D12Device,
    buffer: impl Fn(u64) -> Option<ID3D12Resource>,
    reflection_hold: bool,
) -> windows::core::Result<Pass> {
    let mut root: Option<ID3D12RootSignature> = None;
    let mut indirect_root: Option<ID3D12RootSignature> = None;
    let mut pipeline = |part: &str, entry| -> windows::core::Result<ID3D12PipelineState> {
        let code = crate::host::compile(&format!("#define {part}\n{SOURCE}"), entry, s!("cs_5_1"))?;
        let slot = if part == "HOLD_INDIRECT" { &mut indirect_root } else { &mut root };
        // SAFETY: the root signature is inside the shader; plain creations.
        unsafe {
            if slot.is_none() {
                *slot = Some(device.CreateRootSignature(0, &code)?);
            }
            let desc = D3D12_COMPUTE_PIPELINE_STATE_DESC {
                pRootSignature: std::mem::transmute_copy(slot),
                CS: D3D12_SHADER_BYTECODE { pShaderBytecode: code.as_ptr().cast(), BytecodeLength: code.len() },
                ..Default::default()
            };
            device.CreateComputePipelineState(&desc)
        }
    };
    let keep = pipeline("KEEP", s!("keep"))?;
    let mean = pipeline("KEEP", s!("mean"))?;
    let place = pipeline("PLACE", s!("place"))?;
    let gather = pipeline("GATHER", s!("gather"))?;
    let raise = pipeline("RAISE", s!("raise"))?;
    let air = pipeline("AIR", s!("clear_air"))?;
    let hold = if reflection_hold {
        pipeline("HOLD", s!("hold"))
            .inspect_err(|e| tracing::warn!(%e, "no hold after direct reflection resolves"))
            .ok()
    } else {
        None
    };
    let hold_indirect = if reflection_hold {
        pipeline("HOLD_INDIRECT", s!("hold"))
            .inspect_err(|e| tracing::warn!(%e, "no hold after indirect reflection resolves"))
            .ok()
    } else {
        None
    };
    let made = |bytes| buffer(bytes).ok_or_else(windows::core::Error::empty);
    Ok(Pass {
        root: root.ok_or_else(windows::core::Error::empty)?,
        keep,
        mean,
        place,
        gather,
        raise,
        air,
        hold,
        hold_indirect: indirect_root.zip(hold_indirect),
        cells: made(CELL_BYTES)?,
        points: made(POINT_BYTES)?,
    })
}

fn pass() -> Option<&'static Pass> {
    static PASS: OnceLock<Option<Pass>> = OnceLock::new();
    PASS.get_or_init(|| {
        let host = host()?;
        make(&host.device, |bytes| host.uav_buffer(bytes), !reflect_in().is_empty())
            .inspect_err(|e| tracing::warn!(error = %e, "no stand-in for the indirect light"))
            .ok()
    })
    .as_ref()
}

fn uav_barrier(list: &ID3D12GraphicsCommandList) {
    let barrier = D3D12_RESOURCE_BARRIER { Type: D3D12_RESOURCE_BARRIER_TYPE_UAV, ..Default::default() };
    // SAFETY: an open list; the barrier names no resource.
    unsafe { list.ResourceBarrier(&[barrier]) };
}

/// How a resolve of the reflections was dispatched.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub(crate) enum Dispatched {
    /// `Dispatch`: held after it.
    Direct,
    /// A GPU-gated hold was recorded after one uncounted indirect record.
    Indirect,
    /// `ExecuteIndirect` with a count buffer: not held.
    Counted,
    /// `Dispatch` of no groups: not held.
    Empty,
    /// An uncounted call with more than one record was left alone.
    Multiple,
}

static BY_DISPATCH: [std::sync::atomic::AtomicU64; 5] = [const { std::sync::atomic::AtomicU64::new(0) }; 5];

/// Whether a reflection hold would be recorded after a resolve now.
pub(crate) fn holds() -> bool {
    use std::sync::atomic::Ordering::Relaxed;
    let constant = match set() {
        Set::Off => return false,
        Set::Estimate(_) => false,
        Set::Constant(_) => true,
    };
    pass().is_some_and(|p| p.hold.is_some() || p.hold_indirect.is_some())
        && !crate::pipeline::builder_runs()
        && (constant || KEPT.load(Relaxed))
}

/// Logs reflection hold statistics periodically.
pub(crate) fn note_reflect(how: Dispatched) {
    use std::sync::atomic::{AtomicBool, Ordering::Relaxed};
    if how == Dispatched::Counted {
        static SAID: AtomicBool = AtomicBool::new(false);
        if !SAID.swap(true, Relaxed) {
            tracing::info!("reflections not held after a counted indirect dispatch");
        }
    }
    if how == Dispatched::Empty {
        static SAID: AtomicBool = AtomicBool::new(false);
        if !SAID.swap(true, Relaxed) {
            tracing::info!("reflections not held after a dispatch of no groups");
        }
    }
    let total = BY_DISPATCH[how as usize].fetch_add(1, Relaxed) + 1;
    if total == 1 || total.is_multiple_of(1000) {
        let [direct, indirect_gated, counted, empty, multiple] = BY_DISPATCH.each_ref().map(|n| n.load(Relaxed));
        tracing::info!(
            direct,
            indirect_gated,
            counted,
            empty,
            multiple,
            "reflections' resolves: direct holds, GPU-gated indirect holds, and skipped resolves"
        );
    }
}

/// Whether the light of a frame has been kept: the estimate has something
/// to begin with.
static KEPT: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);

/// What the passes read and write, as views in the heap the list has set.
pub(crate) struct Views {
    /// The image the title's program just wrote.
    pub(crate) image: D3D12_GPU_DESCRIPTOR_HANDLE,
    /// The scene's depth, for [`Role::Resolve`].
    pub(crate) depth: Option<D3D12_GPU_DESCRIPTOR_HANDLE>,
    /// The size of the image, and how many slices it has.
    pub(crate) size: [u32; 2],
    pub(crate) slices: u32,
}

/// The exact dispatch arguments produced by the indirect clamp.
#[derive(Clone, Copy)]
pub(crate) struct IndirectHold<'a> {
    pub(crate) buffer: &'a ID3D12Resource,
    pub(crate) at: u32,
}

impl IndirectHold<'_> {
    fn run(self, list: &ID3D12GraphicsCommandList, pso: &ID3D12PipelineState, groups: [u32; 2]) {
        let arguments = D3D12_RESOURCE_STATE_INDIRECT_ARGUMENT;
        let readable = D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE;
        // SAFETY: the open list owns the buffer, and the clamp left it in
        // INDIRECT_ARGUMENT. Restore that state for reuse and list teardown.
        unsafe {
            list.ResourceBarrier(&[transition(self.buffer, arguments, readable)]);
            list.SetComputeRoot32BitConstant(0, self.at, 12);
            list.SetComputeRootShaderResourceView(5, self.buffer.GetGPUVirtualAddress());
            list.SetPipelineState(pso);
            list.Dispatch(groups[0], groups[1], 1);
            list.ResourceBarrier(&[transition(self.buffer, readable, arguments)]);
        }
        uav_barrier(list);
    }
}

/// After the title's program of that role, in `list` with the program's view
/// heap set. Replaces the list's compute root signature and pipeline; false
/// if it recorded nothing.
pub(crate) fn after(
    list: &HostList,
    role: Role,
    views: &Views,
    indirect: Option<IndirectHold<'_>>,
) -> bool {
    use std::sync::atomic::Ordering;
    let Some(pass) = pass() else {
        return false;
    };
    let (level, constant) = match set() {
        Set::Off => return false,
        Set::Estimate(level) => (level, 0.0f32),
        Set::Constant(level) => (0.0, level),
    };
    if role != Role::Radiance && constant == 0.0 && !KEPT.load(Ordering::Relaxed) {
        return false;
    }
    if role == Role::Air && (constant > 0.0 || air() == 0.0) {
        return false;
    }
    if role == Role::Reflect {
        let can_hold = if indirect.is_some() { pass.hold_indirect.is_some() } else { pass.hold.is_some() };
        if !can_hold || crate::pipeline::builder_runs() {
            return false;
        }
        static HELD: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);
        if !HELD.swap(true, Ordering::Relaxed) {
            tracing::info!(size = ?views.size, "the reflection hold is enabled");
        }
    }
    static SAID: Mutex<u32> = Mutex::new(0);
    {
        let mut said = SAID.lock().unwrap_or_else(|e| e.into_inner());
        if *said < 4 {
            *said += 1;
            tracing::info!(?role, size = ?views.size, level, constant, "a stand-in for the indirect light");
        }
    }
    list.keep(&pass.cells);
    list.keep(&pass.points);
    let [strength, radius] = shade();
    let floats = [level, constant, strength, radius, 0.1, 0.45];
    let mut words = [0u32; 13];
    for (to, v) in words.iter_mut().zip(floats) {
        *to = v.to_bits();
    }
    words[6] = views.size[0];
    words[7] = views.size[1];
    words[8] = 0.04f32.to_bits();
    words[9] = match role {
        Role::Reflect => reflect().to_bits(),
        _ => most().to_bits(),
    };
    words[10] = air().to_bits();
    let l = &list.list;
    let indirect = indirect.and_then(|args| pass.hold_indirect.as_ref().map(|(root, pso)| (args, root, pso)));
    let root = indirect.map_or(&pass.root, |(_, root, _)| root);
    // SAFETY: an open list with the heap of the views set; the title's
    // program has just used both views, so their textures are in the states
    // these views need; buffers the list keeps.
    unsafe {
        uav_barrier(l);
        l.SetComputeRootSignature(root);
        l.SetComputeRoot32BitConstants(0, words.len() as u32, words.as_ptr().cast(), 0);
        if let Some(depth) = views.depth {
            l.SetComputeRootDescriptorTable(1, depth);
        }
        l.SetComputeRootDescriptorTable(2, views.image);
        l.SetComputeRootUnorderedAccessView(3, pass.cells.GetGPUVirtualAddress());
        l.SetComputeRootUnorderedAccessView(4, pass.points.GetGPUVirtualAddress());
        let run = |pso: &ID3D12PipelineState, x, y, z| {
            l.SetPipelineState(pso);
            l.Dispatch(x, y, z);
            uav_barrier(l);
        };
        match role {
            Role::Radiance => {
                run(&pass.keep, 8, 9, 1);
                run(&pass.mean, 1, 1, 1);
                KEPT.store(true, Ordering::Relaxed);
            }
            Role::Air => run(&pass.air, views.size[0].div_ceil(8), views.size[1].div_ceil(8), views.slices.div_ceil(2)),
            Role::Reflect => {
                let groups = views.size.map(|size| size.div_ceil(8));
                if let Some((args, _, pso)) = indirect {
                    args.run(l, pso, groups);
                } else if let Some(hold) = &pass.hold {
                    run(hold, groups[0], groups[1], 1);
                }
            }
            Role::Resolve => {
                if constant == 0.0 {
                    run(&pass.place, 8, 9, 1);
                    run(&pass.gather, 20, 45, 1);
                }
                run(&pass.raise, views.size[0].div_ceil(8), views.size[1].div_ceil(8), 1);
            }
        }
    }
    true
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn settings() {
        assert_eq!(parse(""), Set::Off);
        assert_eq!(parse("off"), Set::Off);
        assert_eq!(parse("0"), Set::Off);
        assert_eq!(parse("0.8"), Set::Estimate(0.8));
        assert_eq!(parse("constant:0.3"), Set::Constant(0.3));
        assert_eq!(parse("constant:nan"), Set::Off);
        assert_eq!(parse_reflect(""), 0.0);
        assert_eq!(parse_reflect("off"), 0.0);
        assert_eq!(parse_reflect("0"), 0.0);
        assert_eq!(parse_reflect("-1"), 0.0);
        assert_eq!(parse_reflect("nan"), 0.0);
        assert_eq!(parse_reflect("1.15"), 1.15);
        assert!(parse_reflect_in("").is_empty());
        assert!(parse_reflect_in(" , ,").is_empty());
        assert_eq!(parse_reflect_in("CS_TemporalResolve2_Opaque, CS_TemporalResolve1_Alpha ,"), [
            "CS_TemporalResolve2_Opaque",
            "CS_TemporalResolve1_Alpha",
        ]);
    }

    #[test]
    fn roles_by_name() {
        assert_eq!(role(b"..CS_ScreenProbes_TemporalResolve_a911b49c_Wrapped.."), Some(Role::Resolve));
        assert_eq!(role(b"..CS_CacheScreenSpaceRadiance_eed2fc35.."), Some(Role::Radiance));
        assert_eq!(role(b"CS_AccumulateLocalLightInscatteringVolume_7e2f5eef"), Some(Role::Air));
        let reflect = ["CS_TemporalResolve2_Opaque".to_owned(), "CS_TemporalResolve1_Alpha".to_owned()];
        assert_eq!(role_in(b"..CS_TemporalResolve2_Opaque_1234..", &reflect), Some(Role::Reflect));
        assert_eq!(role_in(b"..CS_TemporalResolve1_Alpha..", &reflect), Some(Role::Reflect));
        assert_eq!(role_in(b"..CS_TemporalResolve2_Opaque..", &[]), None);
        assert_eq!(role(b"CS_TemporalResolve"), None);
    }

    /// The passes (including direct & indirect reflection holds) compile and build.
    #[test]
    fn the_passes_build() {
        let Some(host) = host() else {
            return;
        };
        let p = make(&host.device, |bytes| host.uav_buffer(bytes), true).expect("the passes");
        assert!(p.hold.is_some());
        assert!(p.hold_indirect.is_some());
    }
}
