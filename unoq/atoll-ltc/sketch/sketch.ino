// Atoll LTC timecode generator -- MCU side (Arduino UNO Q, STM32U585 / Zephyr).
//
// The Linux side keeps CLOCK_REALTIME locked to the Atoll PTP grandmaster (ptp4l)
// and pushes a "time_sync" notification once a second over the Bridge. This side
// turns that into a free-running, phase-aligned 25 fps SMPTE/EBU LTC bitstream
// on PIN_LTC, a 1 ms pulse at every frame start on PIN_FRAME and a 100 ms pulse
// at the top of every second on PIN_PPS. Modulino Buttons A/B/C fire IS-05 takes on
// the Atoll panel (via Python) and their LEDs act as tally for the on-air source.
//
// Timing: a Zephyr k_timer fires every kernel tick (100 us). One LTC bit is
// 500 us = 5 ticks; the biphase-mark "1" mid-bit transition lands at tick 2
// (200 us) which every LTC reader accepts (threshold is 75% of a bit).
// The wall clock is modelled as (tick_count + wall_offset) in 100 us units;
// the sync handler steps wall_offset on first lock and afterwards slews it by
// at most four ticks per frame (one bit shortened/lengthened by 100 us at bits
// 0/20/40/60), which covers the MCU oscillator's ~0.3% rate error without glitches.

#include <Arduino_RouterBridge.h>
#include <Arduino_LED_Matrix.h>
#include <Modulino.h>
#include <zephyr/kernel.h>

static const int PIN_LTC   = 2;
static const int PIN_FRAME = 3;
static const int PIN_PPS   = 4;

static const int32_t FPS             = 25;
static const int32_t TICKS_PER_BIT   = 5;      // 500 us
static const int32_t HALF_BIT_TICK   = 2;      // 200 us
static const int32_t TICKS_PER_FRAME = 400;    // 40 ms
static const int32_t TICKS_PER_SEC   = 10000;
static const int32_t PPS_TICKS       = 1000;   // 100 ms
static const int32_t FRAME_PULSE_TICKS = 10;   // 1 ms
static const int32_t STEP_THRESHOLD  = 200;    // 20 ms: step instead of slew

// ---- shared state (ISR <-> main thread) ----
static struct k_timer ltc_timer;
static volatile int64_t tick_count = 0;      // ticks since boot
static volatile int64_t wall_offset = 0;     // wall ticks = tick_count + wall_offset
static volatile int32_t pending_adj = 0;     // ticks still to slew into wall_offset
static volatile bool    resync = true;       // recompute frame/position from wall
static volatile bool    synced = false;      // have we ever had a time_sync?
static volatile bool    ptp_locked = false;
static volatile int32_t tz_offset_s = 0;
static volatile uint32_t last_sync_ms = 0;
static volatile int32_t last_err_ticks = 0;
static volatile uint32_t sync_count = 0;
static volatile float rate_est = 0.0f;       // MCU clock rate error, wall ticks gained per second
static float rate_acc = 0.0f;                // ISR accumulator (ticks)
static volatile uint32_t slew_steps = 0;     // diagnostics
static volatile uint32_t missed_ticks = 0;

// ISR-private
static int64_t cur_frame = -1;   // frame number since epoch
static int32_t pos = 0;          // tick position inside the frame 0..399
static uint8_t bits[80];
static uint8_t cur_bit = 0;
static bool ltc_level = false;
static int32_t frame_pulse_left = 0;
static int32_t pps_left = 0;
static int32_t slews_this_frame = 0;   // up to MAX_SLEWS_PER_FRAME, at bits 0/20/40/60
static const int32_t MAX_SLEWS_PER_FRAME = 4;   // 100 ticks/s = 1% rate capacity
static volatile uint8_t disp_hh, disp_mm, disp_ss, disp_ff;

static Arduino_LED_Matrix matrix;
static ModulinoPixels pixels;
static ModulinoButtons buttons;
static bool have_pixels = false;
static bool have_buttons = false;
static bool btn_prev[3] = {false, false, false};

// Build the 80-bit LTC word for absolute frame number f (25 fps, EBU layout).
static void encode_frame(int64_t f) {
    int64_t sec = f / FPS;
    uint8_t ff = (uint8_t)(f % FPS);
    int64_t sod = (sec + tz_offset_s) % 86400; if (sod < 0) sod += 86400;
    uint8_t hh = sod / 3600, mm = (sod / 60) % 60, ss = sod % 60;
    disp_hh = hh; disp_mm = mm; disp_ss = ss; disp_ff = ff;

    memset(bits, 0, sizeof(bits));
    auto put = [](int start, int n, uint8_t v) { for (int i = 0; i < n; i++) bits[start + i] = (v >> i) & 1; };
    put(0, 4, ff % 10);  put(8, 2, ff / 10);
    put(16, 4, ss % 10); put(24, 3, ss / 10);
    put(32, 4, mm % 10); put(40, 3, mm / 10);
    put(48, 4, hh % 10); put(56, 2, hh / 10);
    bits[10] = 0;  // drop frame
    bits[11] = 0;  // colour frame
    bits[58] = 1;  // BGF1: time-of-day clock
    // sync word 0011 1111 1111 1101 in bits 64..79
    static const uint8_t sw[16] = {0,0,1,1,1,1,1,1,1,1,1,1,1,1,0,1};
    for (int i = 0; i < 16; i++) bits[64 + i] = sw[i];
    // bit 59 (25 fps): biphase polarity correction -> even number of zeros
    int zeros = 0; for (int i = 0; i < 80; i++) if (!bits[i]) zeros++;
    bits[59] = (zeros & 1) ? 1 : 0;
}

static inline void set_ltc(bool lvl) { ltc_level = lvl; digitalWrite(PIN_LTC, lvl ? HIGH : LOW); }

// Runs in ISR context every 100 us.
static void ltc_tick(struct k_timer *t) {
    uint32_t n = k_timer_status_get(t);   // expiries since last call (>1 = we were late)
    if (n > 1) missed_ticks += n - 1;
    tick_count += n ? n : 1;
    if (!synced) return;                       // idle until the first sync

    if (resync) {
        int64_t w = tick_count + wall_offset;
        cur_frame = w / TICKS_PER_FRAME;
        pos = (int32_t)(w % TICKS_PER_FRAME);
        encode_frame(cur_frame);
        resync = false;
        // fall through and render this tick at its true position
    } else {
        pos++;
        if (pos >= TICKS_PER_FRAME) { pos = 0; cur_frame++; encode_frame(cur_frame); }
    }

    if (pos == 0) {
        slews_this_frame = 0;
        rate_acc += rate_est / (float)FPS;           // feed-forward oscillator rate error
        while (rate_acc >= 1.0f)  { pending_adj++; rate_acc -= 1.0f; }
        while (rate_acc <= -1.0f) { pending_adj--; rate_acc += 1.0f; }
        frame_pulse_left = FRAME_PULSE_TICKS; digitalWrite(PIN_FRAME, HIGH);
        if (cur_frame % FPS == 0) { pps_left = PPS_TICKS; digitalWrite(PIN_PPS, HIGH); }
    }
    int32_t bit = pos / TICKS_PER_BIT, phase = pos % TICKS_PER_BIT;
    if (phase == 0) { cur_bit = bits[bit]; set_ltc(!ltc_level); }
    else if (phase == HALF_BIT_TICK && cur_bit) set_ltc(!ltc_level);
    else if (phase == 3 && pending_adj != 0 && (bit % 20) == 0 && slews_this_frame < MAX_SLEWS_PER_FRAME) {
        slews_this_frame++; slew_steps++;
        // Slew one tick per frame, at a phase where a skip/repeat is harmless.
        if (pending_adj > 0) { wall_offset++; pending_adj--; pos++; }
        else                 { wall_offset--; pending_adj++; pos--; }
    }

    if (frame_pulse_left > 0 && --frame_pulse_left == 0) digitalWrite(PIN_FRAME, LOW);
    if (pps_left > 0 && --pps_left == 0) digitalWrite(PIN_PPS, LOW);
}

// Bridge handler: wall time (UTC seconds + ns) as seen by the PTP-locked Linux clock,
// plus the one-way Bridge latency estimate in microseconds, lock flag, tz offset.
static bool time_sync(uint32_t sec, uint32_t ns, uint32_t latency_us, bool locked, int32_t tz_s) {
    int64_t wall_ticks = ((int64_t)sec * 1000000000LL + ns + (int64_t)latency_us * 1000LL) / 100000LL;
    unsigned int key = irq_lock();
    int64_t est = tick_count + wall_offset;
    int64_t err = wall_ticks - est;
    ptp_locked = locked; tz_offset_s = tz_s;
    if (!synced || err > STEP_THRESHOLD || err < -STEP_THRESHOLD) {
        wall_offset += err; pending_adj = 0; resync = true; synced = true;
    } else {
        // Residual after last second's feed-forward = rate estimate error. Learn it slowly.
        static uint32_t prev_ms = 0; uint32_t now_ms = millis();
        if (prev_ms && now_ms - prev_ms > 500 && now_ms - prev_ms < 3000) {
            float per_sec = (float)(err + pending_adj) * 1000.0f / (float)(now_ms - prev_ms);
            rate_est += per_sec * 0.5f;             // integrate the residual into the rate estimate
        }
        prev_ms = now_ms;
        pending_adj = (int32_t)err;
    }
    last_err_ticks = (int32_t)err;
    irq_unlock(key);
    last_sync_ms = millis(); sync_count++;
    return true;
}

static int ping() { return 1; }

// Tally: Python lights the LED under the button whose source is on air.
static bool set_button_leds(bool a, bool b, bool c) {
    if (have_buttons) buttons.setLeds(a, b, c);
    return have_buttons;
}

static void poll_buttons() {
    if (!have_buttons || !buttons.update()) return;
    static const char names[3] = {'A', 'B', 'C'};
    for (int i = 0; i < 3; i++) {
        bool now = buttons.isPressed(names[i]) == HIGH;
        if (now && !btn_prev[i]) Bridge.notify("button", String(names[i]));   // press edge only
        btn_prev[i] = now;
    }
}

static String i2c_scan() {
    String out;
    HardwareI2C* w = Module::getWire();
    if (!w) return "nowire";
    for (uint8_t a = 1; a < 0x78; a++) {
        w->beginTransmission(a);
        if (w->endTransmission() == 0) { out += String(a, HEX); out += ","; }
    }
    return out.length() ? out : "none";
}

static String get_status() {
    char buf[160];
    snprintf(buf, sizeof(buf), "%02u:%02u:%02u:%02u locked=%d synced=%d err=%ld syncs=%lu slews=%lu missed=%lu ticks=%lld ms10=%lu pend=%ld rate=%.1f/s pixels=%d buttons=%d i2c=%s",
             disp_hh, disp_mm, disp_ss, disp_ff, (int)ptp_locked, (int)synced, (long)last_err_ticks, (unsigned long)sync_count,
             (unsigned long)slew_steps, (unsigned long)missed_ticks, (long long)tick_count, (unsigned long)millis() * 10UL, (long)pending_adj, (double)rate_est, (int)have_pixels, (int)have_buttons, i2c_scan().c_str());
    return String(buf);
}

void setup() {
    pinMode(PIN_LTC, OUTPUT); pinMode(PIN_FRAME, OUTPUT); pinMode(PIN_PPS, OUTPUT);
    digitalWrite(PIN_LTC, LOW); digitalWrite(PIN_FRAME, LOW); digitalWrite(PIN_PPS, LOW);
    matrix.begin(); matrix.setGrayscaleBits(3);
    Modulino.begin();
    have_pixels = pixels.begin();
    if (have_pixels) { pixels.clear(); pixels.show(); }
    have_buttons = buttons.begin();
    if (have_buttons) buttons.setLeds(false, false, false);

    k_timer_init(&ltc_timer, ltc_tick, NULL);
    k_timer_start(&ltc_timer, K_TICKS(1), K_TICKS(1));

    Bridge.begin();
    Bridge.provide("time_sync", time_sync);
    Bridge.provide("ping", ping);
    Bridge.provide("get_status", get_status);
    Bridge.provide("set_button_leds", set_button_leds);
}

static void draw_matrix() {
    static uint8_t fb[104];
    memset(fb, 0, sizeof(fb));
    bool fresh = synced && (millis() - last_sync_ms) < 3000;
    if (!synced) { fb[3 * 13 + 6] = 2; matrix.draw(fb); return; }   // single dim dot: waiting
    // Row 0..1: sweeping frame-of-second column. Rows 3..7: seconds as a 60-step bar (13 cols x 4 rows = 52 -> scale).
    int col = (disp_ff * 13) / FPS;
    fb[col] = 7; fb[13 + col] = 7;
    int lit = (disp_ss * 52) / 60;
    for (int i = 0; i < lit; i++) fb[3 * 13 + i] = fresh ? (ptp_locked ? 4 : 1) : 1;
    matrix.draw(fb);
}

static void draw_pixels() {
    if (!have_pixels) return;
    bool fresh = synced && (millis() - last_sync_ms) < 3000;
    pixels.clear();
    pixels.set(0, ptp_locked ? GREEN : RED, 20);            // PTP lock
    pixels.set(1, fresh ? GREEN : YELLOW, 20);              // Bridge sync freshness
    pixels.set(2, synced ? BLUE : BLACK, disp_ff < 2 ? 40 : 5); // frame-0 blink
    int e = last_err_ticks < 0 ? -last_err_ticks : last_err_ticks;   // |err| in 100us
    pixels.set(3, e < 10 ? GREEN : (e < 50 ? YELLOW : RED), 10);    // phase error <1ms / <5ms
    pixels.show();
}

void loop() {
    Bridge.update();
    static uint32_t last_btn = 0;
    if (millis() - last_btn >= 20) { last_btn = millis(); poll_buttons(); }
    static uint32_t last_probe = 0;   // hot-plug: keep looking for the Buttons module
    if (!have_buttons && millis() - last_probe >= 2000) {
        last_probe = millis();
        have_buttons = buttons.begin();
        if (have_buttons) buttons.setLeds(false, false, false);
    }
    static uint32_t last_draw = 0;
    if (millis() - last_draw >= 40) { last_draw = millis(); draw_matrix(); draw_pixels(); }
}
