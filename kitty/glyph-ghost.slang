#language slang 2026
import kitty_custom_shader_types;

// Deleted letters float up and fade away like ghosts. Works on kitty 0.49.x and newer.
//
// A deleted letter is already gone from the screen on the very next frame, so this shader
// remembers what the screen looked like. The pipeline (glyph-pop.pipeline) runs this one
// file three times, and MODE says which job a run does:
//   0  copy     copies what persist held last frame into t.a (a group cannot read the
//               texture it is writing to, and the next job needs last frame's picture)
//   1  capture  writes persist: the screen as kitty drew it, except that cells where a
//               letter was just deleted keep last frame's picture, so the dead letter
//               stays available for as long as it is floating
//   2  draw     draws every dead letter on top of the screen, floating up and fading
//
// kitty 0.49 only reports the cursor while its trail is still moving, so there is no move
// history to read. Instead the shader keeps its own list of ghosts in the first SLOTS + 2
// pixels of the bottom row of persist (those pixels are not used for the picture):
//   pixel 0        rgb = corner of the last cursor cell seen, next ghost slot to use
//   pixel 1        rgb = cell size (UV), 1 while any ghost is alive
//   pixel 2 + j    rgb = corner of ghost j's cell, life left (1 = just deleted, 0 = free)
static const int MODE = 2;

// ---------------------------------------------------------------- the look (all tweakable)
static const float LIFE = 2.6;          // seconds from deletion until the letter has vanished
                                        // (keep animation_stop in the pipeline a bit above this)
static const float RISE = 2.0;          // how far it floats up, in lines of text
static const float RISE_CURVE = 1.7;    // 1 = steady rise, higher = quick lift-off, then a slow drift
static const float SWAY = 0.22;         // side to side drift, in letter widths
static const float SWAY_HZ = 0.8;       // how many sways per second
static const float SHRINK = 0.25;       // how much smaller it is by the end (0.25 = 25% smaller)
static const float BLUR = 0.40;         // how soft it is by the end, in letter widths (0 = stays sharp)
static const float START_ALPHA = 0.95;  // how solid it is the moment it is deleted
static const float HOLD = 0.10;         // fraction of its life it stays solid before it starts fading
static const float FADE_CURVE = 1.25;   // higher = fades out faster towards the end
static const float DEATH_DESAT = 0.85;  // how much colour drains out of it by the end (0 = keeps its colour)
static const float3 DEATH_TINT = float3(0.78, 0.90, 1.00);   // what it drains towards: a cold, pale glow
// Only SLOTS ghosts exist at once. When you hold backspace the oldest ghosts would be
// dropped abruptly, so ghosts that are 5 or more deletions old fade out gracefully instead.
static const float HAND_OFF_FROM = 4.5;
static const float HAND_OFF_TO = 7.5;

// ---------------------------------------------------------------- finding the dead letters
// A cell counts as empty when the 30 points inside it that the cursor beam cannot touch are all
// the same colour. Under a block cursor that colour is the cursor's, which works the same way.
// Raise this if a background image or gradient makes empty cells look different.
static const float BLANK_TOLERANCE = 0.03;

static const float TWO_PI = 6.28318530718;
static const int SLOTS = 8;
static const float SAME_SPOT = 1.0e-4;

// ---------------------------------------------------------------- the ghost list
struct Meta {
    float2 last;    // corner of the last cursor cell that was looked at
    int next;       // slot the next deleted letter goes into
    float2 cell;    // size of a cell (UV)
    float alive;    // above 0 while any ghost is alive
};

float2 meta_pos(int i, float2 px) {
    return float2((float(i) + 0.5) / px.x, 0.5 / px.y);
}

Meta read_meta(KittyTexture tex, float2 px) {
    float3 h0 = tex.Sample(meta_pos(0, px)).rgb;
    float3 h1 = tex.Sample(meta_pos(1, px)).rgb;
    Meta m;
    m.last = h0.xy;
    m.next = int(floor(h0.z * float(SLOTS) + 0.5)) % SLOTS;
    m.cell = h1.xy;
    m.alive = h1.z;
    return m;
}

float3 read_slot(KittyTexture tex, int j, float2 px) {
    return tex.Sample(meta_pos(2 + j, px)).rgb;
}

bool inside(float2 p, float2 lo, float2 hi) {
    return all(p >= lo) && all(p <= hi);
}

// ---------------------------------------------------------------- the cursor
// The corner of the cursor's cell, valid only while kitty reports the cursor.
bool cursor_known(KittyCustomShaderData d) {
    return d.cursor_trail_edge.y > d.cursor_trail_edge.x;
}

float2 cursor_corner(KittyCustomShaderData d) {
    return float2(d.cursor_trail_edge.x, d.cursor_trail_edge.w);
}

struct Step {
    bool valid;
    float2 lo;      // lower-left corner of the cell the letter was in (UV)
    float2 hi;      // upper-right corner
};

// The latest cursor move is a deleted letter when the cursor stepped exactly one cell to the
// left on the same row: the letter that was there is now gone. A beam is as tall as a cell and
// its left edge is the cell's left edge, so the step is measured from left edge to left edge.
Step find_step(KittyCustomShaderData d, float2 px) {
    Step s;
    s.valid = false;
    s.lo = float2(0.0);
    s.hi = float2(0.0);
    float4 now = d.cursor_trail_edge;
    float4 prev = d.cursor_trail_prev_edge;
    if (now.y <= now.x || prev.y <= prev.x) return s;

    float cell_w = (prev.x - now.x) * px.x;      // pixels the cursor moved to the left
    float cell_h = (now.z - now.w) * px.y;
    if (cell_w < 2.0 || cell_w > 0.85 * cell_h) return s;                // not a one cell step left
    if (abs(now.w - prev.w) * px.y > 0.5 * cell_h) return s;             // moved to another row

    s.valid = true;
    s.lo = float2(now.x, now.w);
    s.hi = float2(prev.x, now.z);
    return s;
}

float frame_dt(KittyCustomShaderData d) {
    return max(0.0, d.timestamp - d.last_rendered_at);
}

bool cell_is_empty(KittyTexture tex, float2 lo, float2 hi) {
    float2 size = hi - lo;
    float4 base = tex.Sample(lo + size * float2(0.92, 0.90));
    [ForceUnroll]
    for (int iy = 0; iy < 6; iy++) {
        [ForceUnroll]
        for (int ix = 0; ix < 5; ix++) {
            float2 u = float2(0.28 + 0.16 * float(ix), 0.10 + 0.16 * float(iy));
            float4 c = tex.Sample(lo + size * u);
            if (any(abs(c - base) > BLANK_TOLERANCE)) return false;
        }
    }
    return true;
}

// A letter died this frame: the cursor just moved one cell left, the cell is empty now and
// it was not empty in last frame's picture.
bool letter_died(KittyTextures t, KittyCustomShaderData d, Step st, Meta m) {
    if (!st.valid || !cursor_known(d)) return false;
    float2 c = cursor_corner(d);
    if (abs(c.x - m.last.x) < SAME_SPOT && abs(c.y - m.last.y) < SAME_SPOT) return false;
    return cell_is_empty(t.backbuffer, st.lo, st.hi) && !cell_is_empty(t.a, st.lo, st.hi);
}

// Slot j after this frame: aged, freed when it ran out or when something was typed in its cell.
float3 next_slot(KittyTextures t, float2 px, Meta m, int j, float dt, bool died, Step st) {
    if (died && j == m.next) return float3(st.lo, 1.0);
    float3 s = read_slot(t.a, j, px);
    float life = s.z - dt / LIFE;
    if (life <= 0.0) return float3(0.0);
    if (!cell_is_empty(t.backbuffer, s.xy, s.xy + m.cell)) return float3(0.0);
    return float3(s.xy, life);
}

// ---------------------------------------------------------------- MODE 1: capture
float4 write_meta(KittyTextures t, KittyCustomShaderData d, float2 px, int i, Step st) {
    Meta m = read_meta(t.a, px);
    bool died = letter_died(t, d, st, m);

    if (i == 0) {
        float2 last = m.last;
        if (cursor_known(d)) last = cursor_corner(d);
        int nx = m.next;
        if (died) nx = (m.next + 1) % SLOTS;
        return float4(last, float(nx) / float(SLOTS), 1.0);
    }

    float dt = frame_dt(d);
    if (i == 1) {
        float2 cell = m.cell;
        if (died) cell = st.hi - st.lo;
        float alive = 0.0;
        [ForceUnroll]
        for (int j = 0; j < SLOTS; j++) {
            alive = max(alive, next_slot(t, px, m, j, dt, died, st).z);
        }
        return float4(cell, alive, 1.0);
    }

    return float4(next_slot(t, px, m, i - 2, dt, died, st), 1.0);
}

float4 capture(KittyTextures t, KittyCustomShaderData d) {
    float4 now = t.backbuffer.Sample(t.pos);
    float2 px = float2(d.viewport_size_pixels) * t.viewport.zw;
    float2 p = t.pos;

    Step st = find_step(d, px);
    int ix = int(floor(p.x * px.x));
    int iy = int(floor(p.y * px.y));
    if (iy == 0 && ix < SLOTS + 2) return write_meta(t, d, px, ix, st);

    if (t.a.Sample(meta_pos(1, px)).z <= 0.0 && !st.valid) return now;
    Meta m = read_meta(t.a, px);

    // the cell where a letter was deleted this very frame keeps last frame's picture
    if (st.valid && inside(p, st.lo, st.hi) && letter_died(t, d, st, m)) return t.a.Sample(p);

    // cells of the ghosts that are still floating keep their picture while they stay empty.
    // If something is typed there, let go of it.
    float dt = frame_dt(d);
    [ForceUnroll]
    for (int j = 0; j < SLOTS; j++) {
        float3 s = read_slot(t.a, j, px);
        if (s.z - dt / LIFE <= 0.0) continue;
        float2 lo = s.xy;
        float2 hi = s.xy + m.cell;
        if (!inside(p, lo, hi)) continue;
        if (cell_is_empty(t.backbuffer, lo, hi)) return t.a.Sample(p);
    }
    return now;
}

// ---------------------------------------------------------------- MODE 2: draw
float4 draw(float4 color, KittyTextures t, KittyCustomShaderData d) {
    float2 px = float2(d.viewport_size_pixels) * t.viewport.zw;
    if (t.persist.Sample(meta_pos(1, px)).z <= 0.0) return color;

    Meta m = read_meta(t.persist, px);
    float2 p = t.pos;
    float2 size = m.cell;
    float3 add_rgb = float3(0.0);
    float add_a = 0.0;

    [ForceUnroll]
    for (int j = 0; j < SLOTS; j++) {
        float3 s = read_slot(t.persist, j, px);
        if (s.z <= 0.0) continue;

        // how many deletions ago this one happened (0 = the newest)
        float rank = float((m.next - 1 - j + 2 * SLOTS) % SLOTS);
        float shown = 1.0 - smoothstep(HAND_OFF_FROM, HAND_OFF_TO, rank);
        if (shown <= 0.0) continue;

        float2 lo = s.xy;
        float2 hi = lo + size;
        float2 center = (lo + hi) * 0.5;
        float life = 1.0 - s.z;                                      // 0 = just deleted, 1 = gone
        float age = life * LIFE;

        // where the letter is now: lifted up, swayed to the side, a little smaller
        float lift = 1.0 - pow(1.0 - life, RISE_CURVE);
        float phase = frac(lo.x * 91.7) * TWO_PI;                    // every column sways out of step
        float sway = sin(TWO_PI * SWAY_HZ * age + phase) * SWAY * size.x * (0.35 + 0.65 * life);
        float2 offset = float2(sway, RISE * size.y * lift);
        float scale = 1.0 - SHRINK * life;

        // which point of the old letter lands on this pixel
        float2 q = center + (p - center - offset) / scale;
        float2 soft = float2(BLUR * size.x * pow(life, 1.4));        // blur radius, grows as it dies
        if (any(q < lo - soft) || any(q > hi + soft)) continue;      // this pixel is not on the ghost

        // how solid it is: holds, then fades slowly out
        float fade = 1.0 - smoothstep(HOLD, 1.0, life);
        float alpha = START_ALPHA * pow(fade, FADE_CURVE) * shown;
        if (alpha <= 0.0) continue;

        // read the letter from the kept picture, as the difference from the cell's own
        // background, so that only the letter's strokes are drawn. A 3x3 blur makes it dissolve.
        float4 base = t.persist.Sample(lo + size * float2(0.92, 0.90));
        float4 ink = float4(0.0);
        [ForceUnroll]
        for (int oy = -1; oy <= 1; oy++) {
            [ForceUnroll]
            for (int ox = -1; ox <= 1; ox++) {
                float w = (ox == 0 ? 2.0 : 1.0) * (oy == 0 ? 2.0 : 1.0) / 16.0;
                float2 qq = clamp(q + float2(float(ox), float(oy)) * soft, lo, hi);
                ink += w * (t.persist.Sample(qq) - base);
            }
        }

        // as it dies the colour drains out of it, towards a cold pale glow
        float luma = dot(ink.rgb, float3(0.2126, 0.7152, 0.0722));
        float3 drained = lerp(ink.rgb, luma * DEATH_TINT, DEATH_DESAT * smoothstep(0.0, 0.8, life));
        add_rgb += drained * alpha;
        add_a += ink.a * alpha;
    }
    return float4(saturate(color.rgb + add_rgb), saturate(color.a + add_a));
}

public float4
fragment_main(float4 color, KittyTextures t, KittyCustomShaderData d) {
    if (MODE == 0) return t.persist.Sample(t.pos);
    if (MODE == 1) return capture(t, d);
    return draw(color, t, d);
}
