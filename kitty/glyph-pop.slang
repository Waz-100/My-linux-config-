#language slang 2026
import kitty_custom_shader_types;

// Total shake time in seconds. Keep animation_stop in glyph-pop.pipeline (ms) just above this.
static const float DURATION = 2.6;

static const float POP_AMOUNT = 0.2;    // initial zoom punch
static const float SHAKE_X = 0.14;      // horizontal shake, fraction of cell width
static const float SHAKE_Y = 0.05;      // vertical shake, fraction of cell height
static const float TWO_PI = 6.28318530718;

// Shake speed in Hz at the start and at the end, so it slows down as it settles
static const float FREQ_X_START = 22.0;
static const float FREQ_X_END = 7.0;
static const float FREQ_Y_START = 17.0;
static const float FREQ_Y_END = 6.0;

// Fast typing: the shader only knows about the newest cursor move, so instead of
// shaking just that one letter it shakes the last TRAIL cells behind the cursor.
// Each older cell is treated as if it was typed STAGGER seconds earlier than the
// one after it, so the shake rolls along the text like a wave and keeps going
// instead of freezing every time you press another key.
static const int TRAIL = 8;             // cells that shake (the oldest one fades to 0)
static const float STAGGER = 0.07;      // set this near your keystroke interval, in seconds

// Phase of a sine whose frequency falls linearly from f0 to f1 over DURATION
static float
shake_phase(float age, float f0, float f1) {
    return TWO_PI * (f0 * age - (f0 - f1) * age * age / (2.0 * DURATION));
}

public float4
fragment_main(float4 color, KittyTextures t, KittyCustomShaderData d) {
    if (d.cursor_trail_change_time <= 0.0) return color;

    float age = d.timestamp - d.cursor_trail_change_time;
    if (age < 0.0 || age >= DURATION) return color;

    float2 pixel_dims = float2(d.viewport_size_pixels) * t.viewport.zw;

    float4 from = d.cursor_trail_prev_edge;
    float4 to = d.cursor_trail_edge;

    // The typed letter sits where the cursor was. A beam cursor is only a few
    // pixels wide, so take the cell width from how far the cursor moved instead.
    float cell_w = (to.x - from.x) * pixel_dims.x;
    float cell_h = (from.z - from.w) * pixel_dims.y;
    if (cell_w <= 0.0 || cell_w > cell_h) return color;                    // not a one cell step right
    if (abs(to.w - from.w) * pixel_dims.y > cell_h * 0.5) return color;    // moved to another row

    // Cell width in the same units as t.pos, and the row the newest letter is on
    float cell_w_n = to.x - from.x;
    float row_lo = from.w;
    float row_hi = from.z;
    float right = to.x;                      // right edge of the newest letter

    float2 pos = t.pos;
    if (pos.y < row_lo || pos.y > row_hi || pos.x > right) return color;

    // 0 = the letter just typed, 1 = the one before it, and so on
    int i = int(floor((right - pos.x) / cell_w_n));
    if (i >= TRAIL) return color;

    // Older letters are further along in their animation, and weaker, so the
    // far end of the trail has already faded to nothing and never snaps.
    float cell_age = age + float(i) * STAGGER;
    if (cell_age >= DURATION) return color;
    float fade = 1.0 - float(i) / float(TRAIL - 1);

    float2 lo = float2(right - float(i + 1) * cell_w_n, row_lo);
    float2 hi = float2(right - float(i) * cell_w_n, row_hi);

    float2 center = (lo + hi) * 0.5;
    float2 size = hi - lo;

    // Smoothstep fall from 1 to 0: holds the shake strong at first, then eases out
    // to exactly 0 at DURATION, so the letter lands solid with no snap.
    float k = 1.0 - cell_age / DURATION;
    float base = k * k * (3.0 - 2.0 * k);
    float envelope = base * fade;

    float2 wobble = float2(
        sin(shake_phase(cell_age, FREQ_X_START, FREQ_X_END)) * SHAKE_X * size.x,
        sin(shake_phase(cell_age, FREQ_Y_START, FREQ_Y_END) + 0.8) * SHAKE_Y * size.y
    ) * envelope;

    // The zoom pop settles faster than the shake
    float pop_envelope = base * base;
    pop_envelope *= pop_envelope;
    pop_envelope *= fade;
    float scale = 1.0 + POP_AMOUNT * pop_envelope;

    float2 source_pos = center + (pos - center) / scale - wobble;
    source_pos = clamp(source_pos, lo, hi);

    return t.backbuffer.Sample(source_pos);
}
