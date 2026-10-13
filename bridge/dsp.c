/* The numeric kernels the bridge used numpy for, in plain C11, loaded with ctypes by dsp.py.
 *
 * Audio is mono at 24 kHz: int16 PCM on the wire, doubles in [-1, 1) inside. Every function takes explicit
 * lengths; dsp.py checks them against the buffers before calling, so loops here stay inside those bounds.
 * Built without FP contraction (-ffp-contract=off): a fused multiply-add would round differently from numpy.
 * Dot products are compensated (Ogita-Rump-Oishi Dot2), close to correctly rounded like Python's math.sumprod.
 */
#include <math.h>
#include <stddef.h>
#include <stdint.h>

static double clip16(double v) { return v > 32767.0 ? 32767.0 : (v < -32768.0 ? -32768.0 : v); }

/* SAFETY: in and out hold n samples (dsp.to_floats sizes out from in). */
void dsp_i16_to_f64(const int16_t *in, size_t n, double *out) {
    for (size_t i = 0; i < n; i++) out[i] = in[i] / 32768.0;
}

/* SAFETY: x and out hold n samples (dsp.from_floats). */
/* x * g -> int16, rounded half to even (rint in the default rounding mode), clipped. */
void dsp_f64_to_i16_round(const double *x, size_t n, double g, int16_t *out) {
    for (size_t i = 0; i < n; i++) out[i] = (int16_t)clip16(rint(x[i] * g));
}

/* SAFETY: x and out hold n samples (dsp.from_floats_trunc). */
/* x * g -> int16, truncated toward zero, clipped. */
void dsp_f64_to_i16_trunc(const double *x, size_t n, double g, int16_t *out) {
    for (size_t i = 0; i < n; i++) out[i] = (int16_t)clip16(trunc(x[i] * g));
}

/* SAFETY: x holds n samples. */
void dsp_scale(double *x, size_t n, double g) {
    for (size_t i = 0; i < n; i++) x[i] *= g;
}

/* SAFETY: x and g hold n samples (dsp.mul checks the lengths match). */
void dsp_mul(double *x, const double *g, size_t n) {
    for (size_t i = 0; i < n; i++) x[i] *= g[i];
}

/* SAFETY: a and b hold n samples (dsp.dot checks; correlate/xcorr pass windows inside seg). */
/* Compensated sum of a[i] * b[i]. */
double dsp_dot(const double *a, const double *b, size_t n) {
    double s = 0.0, c = 0.0;
    for (size_t i = 0; i < n; i++) {
        double p = a[i] * b[i];
        double pe = fma(a[i], b[i], -p);   /* exact error of the product */
        double t = s + p;
        double z = t - s;
        c += (s - (t - z)) + (p - z) + pe; /* exact error of the sum, plus the product's */
        s = t;
    }
    return s + c;
}

/* SAFETY: x holds n samples, out n / win (dsp.frame_peaks sizes it; win > 0). */
/* out[f] = max |x| over the whole win-sample frame f, for f < n / win. */
void dsp_frame_peaks(const double *x, size_t n, size_t win, double *out) {
    for (size_t f = 0; f < n / win; f++) {
        double m = 0.0;
        for (size_t i = f * win; i < (f + 1) * win; i++) m = fabs(x[i]) > m ? fabs(x[i]) : m;
        out[f] = m;
    }
}

/* SAFETY: x holds n samples. */
double dsp_sumsq_i16(const int16_t *x, size_t n) {
    double s = 0.0;                        /* exact: 32768^2 * n stays far below 2^53 for any real buffer */
    for (size_t i = 0; i < n; i++) s += (double)x[i] * x[i];
    return s;
}

/* SAFETY: n_in >= 1 and n_out >= 1 (dsp.resample returns early otherwise); i + 1 <= last < n_in. */
/* numpy.interp over numpy.linspace(0, n_in - 1, n_out), truncated toward zero, clipped. */
void dsp_resample_i16(const int16_t *in, size_t n_in, int16_t *out, size_t n_out) {
    size_t last = n_in - 1;
    double step = n_out > 1 ? (double)last / (double)(n_out - 1) : 0.0;
    for (size_t k = 0; k < n_out; k++) {
        double p = (k < n_out - 1 || n_out == 1) ? (double)k * step : (double)last;
        double v;
        if (last == 0) {
            v = in[0];
        } else {
            size_t i = (size_t)p < last - 1 ? (size_t)p : last - 1;
            v = (double)(in[i + 1] - in[i]) * (p - (double)i) + in[i];
        }
        out[k] = (int16_t)clip16(trunc(v));
    }
}

/* SAFETY: in and out hold n samples (dsp.fade_out). */
/* x ramped linearly from 1 down to 0 (last sample 0), in float32 and truncated, exactly as numpy did:
 * (x.astype(float32) * linspace(1, 0, n, dtype=float32)).astype(int16). */
void dsp_fade_out_i16(const int16_t *in, size_t n, int16_t *out) {
    double step = n > 1 ? -1.0 / (double)(n - 1) : 0.0;
    for (size_t k = 0; k < n; k++) {
        float r = (n > 1 && k == n - 1) ? 0.0f : (float)((double)k * step + 1.0);
        out[k] = (int16_t)truncf((float)in[k] * r);
    }
}

/* SAFETY: in holds frames * ch samples, out frames (dsp.mono: frames = len // ch). */
/* Interleaved frames -> the channel mean, truncated toward zero. */
void dsp_mono_i16(const int16_t *in, size_t frames, size_t ch, int16_t *out) {
    for (size_t f = 0; f < frames; f++) {
        double s = 0.0;
        for (size_t c = 0; c < ch; c++) s += in[f * ch + c];
        out[f] = (int16_t)trunc(s / (double)ch);
    }
}

/* SAFETY: windows seg[k .. k + n) stay inside nseg; out holds nseg - n + 1 (dsp.correlate). */
/* out[k] = sum(seg[k:k+n] * tmpl) for k in 0..nseg-n (numpy.correlate "valid"). */
void dsp_correlate(const double *seg, size_t nseg, const double *tmpl, size_t n, double *out) {
    for (size_t k = 0; k + n <= nseg; k++) out[k] = dsp_dot(seg + k, tmpl, n);
}

/* SAFETY: as dsp_correlate; ref holds n. */
/* Normalized cross-correlation of ref against every window of seg. */
void dsp_xcorr_norm(const double *seg, size_t nseg, const double *ref, size_t n, double *out) {
    double nref = sqrt(dsp_dot(ref, ref, n));
    for (size_t k = 0; k + n <= nseg; k++)
        out[k] = dsp_dot(seg + k, ref, n) / (sqrt(dsp_dot(seg + k, seg + k, n)) * nref + 1e-12);
}

/* SAFETY: a and b hold dim samples (dsp_dtw passes one frame of each sequence). */
static double dist(const double *a, const double *b, size_t dim) {
    double s = 0.0, c = 0.0;               /* compensated sum of squared differences */
    for (size_t i = 0; i < dim; i++) {
        double d = a[i] - b[i];
        double p = d * d;
        double pe = fma(d, d, -p);
        double t = s + p;
        double z = t - s;
        c += (s - (t - z)) + (p - z) + pe;
        s = t;
    }
    return sqrt(s + c);
}

/* SAFETY: a holds n * dim, b m * dim, acc (n + 1) * (m + 1), path 2 * (n + m) ints (dsp.dtw_path sizes them,
 * n, m >= 1); the backtrack takes at most n + m - 1 steps. */
/* DTW (Euclidean, steps (1,0) (0,1) (1,1)) between a (n x dim) and b (m x dim), row-major.
 * acc: scratch of (n + 1) * (m + 1). path: room for n + m pairs (i, j), written from (0, 0). Returns the count. */
size_t dsp_dtw(const double *a, size_t n, const double *b, size_t m, size_t dim, double *acc, int32_t *path) {
    size_t w = m + 1;
    for (size_t j = 0; j < w; j++) acc[j] = INFINITY;
    acc[0] = 0.0;
    for (size_t i = 1; i <= n; i++) {
        acc[i * w] = INFINITY;
        for (size_t j = 1; j <= m; j++) {
            double up = acc[(i - 1) * w + j], left = acc[i * w + j - 1], diag = acc[(i - 1) * w + j - 1];
            double best = up < left ? up : left;
            best = diag < best ? diag : best;
            acc[i * w + j] = dist(a + (i - 1) * dim, b + (j - 1) * dim, dim) + best;
        }
    }
    size_t i = n, j = m, len = 0;
    while (i > 0 && j > 0) {
        path[2 * len] = (int32_t)(i - 1);
        path[2 * len + 1] = (int32_t)(j - 1);
        len++;
        double d = acc[(i - 1) * w + j - 1], u = acc[(i - 1) * w + j], l = acc[i * w + j - 1];
        if (d <= u && d <= l) { i--; j--; }   /* first minimum wins: diagonal, then up, then left */
        else if (u <= l) i--;
        else j--;
    }
    for (size_t k = 0; k < len / 2; k++) { /* reverse into (0, 0) -> (n-1, m-1) order */
        int32_t ti = path[2 * k], tj = path[2 * k + 1];
        path[2 * k] = path[2 * (len - 1 - k)];
        path[2 * k + 1] = path[2 * (len - 1 - k) + 1];
        path[2 * (len - 1 - k)] = ti;
        path[2 * (len - 1 - k) + 1] = tj;
    }
    return len;
}

/* SAFETY: idx and vals hold k entries (dsp.sparse_dot checks); every w index is bounds-checked below. */
/* sum over k of vals[k] * w[idx[k] * stride + col]: one output of a hashed sparse linear layer.
 * NAN if an index falls outside w (w_len floats). */
double dsp_sparse_dot(const float *w, size_t w_len, size_t stride, size_t col, const int64_t *idx,
                      const double *vals, size_t k) {
    double s = 0.0;
    for (size_t i = 0; i < k; i++) {
        if (idx[i] < 0 || (size_t)idx[i] * stride + col >= w_len) return NAN;
        s += vals[i] * (double)w[(size_t)idx[i] * stride + col];
    }
    return s;
}
