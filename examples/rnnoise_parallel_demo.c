/*
 * rnnoise_parallel_demo.c - Multi-threaded parallel RNNoise denoising
 *
 * Splits large PCM files into overlapping segments, processes each segment
 * in a separate thread with its own RNNoise state, then reassembles with
 * cosine crossfade to eliminate splice artifacts.
 *
 * Usage: rnnoise_parallel_demo <input.pcm> <output.pcm> [model.bin] [max_threads]
 *
 * PCM format: s16le, 48kHz, mono
 *
 * Thresholds:
 *   - Files < 5 minutes: single-threaded (no benefit from parallelism)
 *   - Files >= 5 minutes: split into up to MAX_THREADS segments
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include <pthread.h>
#include "rnnoise.h"

#define FRAME_SIZE 480
#define SAMPLE_RATE 48000
/* Overlap duration: 50ms = 2400 samples, aligned to FRAME_SIZE = 2400 */
#define OVERLAP_SAMPLES 2400
/* Default max threads */
#define DEFAULT_MAX_THREADS 4
/* Minimum duration (in seconds) to trigger parallel processing */
#define PARALLEL_THRESHOLD_SEC 300  /* 5 minutes */
/* Minimum segment duration (in seconds) to keep parallelism worthwhile */
#define MIN_SEGMENT_SEC 60

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

typedef struct {
    int thread_id;
    /* Input: pointer into the full PCM buffer for this segment */
    const short *input_samples;
    size_t input_num_samples;
    /* Output: allocated by the thread */
    short *output_samples;
    size_t output_num_samples;
    /* Model (shared, read-only after creation) */
    RNNModel *model;
    /* Status */
    int success;
} ThreadArg;

/*
 * Process a single segment using RNNoise.
 * Each thread gets its own DenoiseState (which contains the GRU hidden state).
 * The first frame output is discarded (warmup) to allow the state to settle.
 */
static void *denoise_segment(void *arg) {
    ThreadArg *t = (ThreadArg *)arg;
    DenoiseState *st = rnnoise_create(t->model);
    if (!st) {
        fprintf(stderr, "[thread %d] Failed to create DenoiseState\n", t->thread_id);
        t->success = 0;
        return NULL;
    }

    size_t total = t->input_num_samples;
    /* Number of complete frames */
    size_t num_frames = total / FRAME_SIZE;
    /* We skip the first frame's output (warmup) */
    size_t output_frames = (num_frames > 0) ? num_frames - 1 : 0;
    size_t out_samples = output_frames * FRAME_SIZE;

    /* Handle remaining samples after last complete frame */
    size_t remaining = total - num_frames * FRAME_SIZE;

    /* If there are remaining samples, we process one more partial frame */
    if (remaining > 0 && num_frames > 0) {
        /* total output = output_frames * FRAME_SIZE + remaining */
        out_samples += remaining;
    }

    t->output_samples = (short *)malloc(sizeof(short) * (out_samples > 0 ? out_samples : 1));
    if (!t->output_samples) {
        fprintf(stderr, "[thread %d] Failed to allocate output buffer\n", t->thread_id);
        rnnoise_destroy(st);
        t->success = 0;
        return NULL;
    }
    t->output_num_samples = out_samples;

    float frame_in[FRAME_SIZE];
    float frame_out[FRAME_SIZE];
    size_t write_pos = 0;
    int warmup = 1;

    /* Process complete frames */
    for (size_t f = 0; f < num_frames; f++) {
        const short *src = t->input_samples + f * FRAME_SIZE;
        for (int i = 0; i < FRAME_SIZE; i++) {
            frame_in[i] = (float)src[i];
        }
        rnnoise_process_frame(st, frame_out, frame_in);
        if (!warmup) {
            for (int i = 0; i < FRAME_SIZE; i++) {
                t->output_samples[write_pos++] = (short)frame_out[i];
            }
        }
        warmup = 0;
    }

    /* Process remaining samples (zero-padded) */
    if (remaining > 0 && num_frames > 0) {
        const short *src = t->input_samples + num_frames * FRAME_SIZE;
        for (size_t i = 0; i < remaining; i++) {
            frame_in[i] = (float)src[i];
        }
        for (size_t i = remaining; i < FRAME_SIZE; i++) {
            frame_in[i] = 0.0f;
        }
        rnnoise_process_frame(st, frame_out, frame_in);
        if (!warmup) {
            for (size_t i = 0; i < remaining; i++) {
                t->output_samples[write_pos++] = (short)frame_out[i];
            }
        }
    }

    t->output_num_samples = write_pos;
    t->success = 1;
    rnnoise_destroy(st);
    return NULL;
}

/*
 * Align a sample count down to the nearest FRAME_SIZE boundary.
 */
static size_t align_to_frame(size_t samples) {
    return (samples / FRAME_SIZE) * FRAME_SIZE;
}

/*
 * Cosine crossfade between the tail of 'left' and the head of 'right'.
 * Writes the blended overlap region into 'out'.
 * overlap_samples: number of samples to crossfade.
 */
static void cosine_crossfade(const short *left_tail, const short *right_head,
                             short *out, size_t overlap_samples) {
    for (size_t i = 0; i < overlap_samples; i++) {
        double alpha = 0.5 * (1.0 - cos(M_PI * (double)i / (double)overlap_samples));
        double blended = (double)left_tail[i] * (1.0 - alpha) + (double)right_head[i] * alpha;
        /* Clamp to int16 range */
        if (blended > 32767.0) blended = 32767.0;
        if (blended < -32768.0) blended = -32768.0;
        out[i] = (short)blended;
    }
}

/*
 * Single-threaded fallback: process entire file sequentially.
 * This is equivalent to rnnoise_wrapper_demo behavior.
 */
static int process_single(const char *input_path, const char *output_path, RNNModel *model) {
    FILE *fin = fopen(input_path, "rb");
    if (!fin) {
        fprintf(stderr, "Failed to open input: %s\n", input_path);
        return 1;
    }
    FILE *fout = fopen(output_path, "wb");
    if (!fout) {
        fprintf(stderr, "Failed to open output: %s\n", output_path);
        fclose(fin);
        return 1;
    }

    DenoiseState *st = rnnoise_create(model);
    if (!st) {
        fprintf(stderr, "Failed to create DenoiseState\n");
        fclose(fin);
        fclose(fout);
        return 1;
    }

    short tmp[FRAME_SIZE];
    float x[FRAME_SIZE];
    int warmup = 1;

    while (1) {
        size_t rd = fread(tmp, sizeof(short), FRAME_SIZE, fin);
        if (rd == 0) break;
        /* Zero-pad last partial frame */
        for (size_t i = rd; i < FRAME_SIZE; i++) tmp[i] = 0;

        for (int i = 0; i < FRAME_SIZE; i++) x[i] = (float)tmp[i];
        rnnoise_process_frame(st, x, x);

        if (!warmup) {
            for (size_t i = 0; i < ((rd == FRAME_SIZE) ? (size_t)FRAME_SIZE : rd); i++) {
                tmp[i] = (short)x[i];
            }
            fwrite(tmp, sizeof(short), (rd == FRAME_SIZE) ? (size_t)FRAME_SIZE : rd, fout);
        }
        warmup = 0;
    }

    rnnoise_destroy(st);
    fclose(fin);
    fclose(fout);
    return 0;
}

int main(int argc, char **argv) {
    if (argc < 3 || argc > 5) {
        fprintf(stderr, "Usage: %s <input.pcm> <output.pcm> [model.bin] [max_threads]\n", argv[0]);
        return 1;
    }

    const char *input_path = argv[1];
    const char *output_path = argv[2];
    const char *model_path = (argc >= 4 && strlen(argv[3]) > 0) ? argv[3] : NULL;
    int max_threads = DEFAULT_MAX_THREADS;
    if (argc >= 5) {
        max_threads = atoi(argv[4]);
        if (max_threads < 1) max_threads = 1;
        if (max_threads > DEFAULT_MAX_THREADS) max_threads = DEFAULT_MAX_THREADS;
    }

    /* Load model if specified */
    RNNModel *model = NULL;
    if (model_path) {
        model = rnnoise_model_from_filename(model_path);
        if (!model) {
            fprintf(stderr, "Failed to load model: %s\n", model_path);
            return 1;
        }
    }

    /* Get file size */
    FILE *fin = fopen(input_path, "rb");
    if (!fin) {
        fprintf(stderr, "Failed to open input: %s\n", input_path);
        if (model) rnnoise_model_free(model);
        return 1;
    }
    fseek(fin, 0, SEEK_END);
    long file_size = ftell(fin);
    fclose(fin);

    if (file_size <= 0) {
        fprintf(stderr, "Empty or invalid input file\n");
        if (model) rnnoise_model_free(model);
        return 1;
    }

    size_t total_samples = (size_t)file_size / sizeof(short);
    double duration_sec = (double)total_samples / SAMPLE_RATE;

    fprintf(stderr, "Input: %.1f seconds (%zu samples, %ld bytes)\n",
            duration_sec, total_samples, file_size);

    /* For short files, use single-threaded processing */
    if (duration_sec < PARALLEL_THRESHOLD_SEC || max_threads <= 1) {
        fprintf(stderr, "Using single-threaded processing\n");
        int ret = process_single(input_path, output_path, model);
        if (model) rnnoise_model_free(model);
        return ret;
    }

    /* ---- Parallel processing ---- */

    /* Determine number of threads: ensure each segment is at least MIN_SEGMENT_SEC */
    int num_threads = max_threads;
    while (num_threads > 1 && (duration_sec / num_threads) < MIN_SEGMENT_SEC) {
        num_threads--;
    }
    if (num_threads < 2) {
        fprintf(stderr, "Segments too short for parallelism, using single-threaded\n");
        int ret = process_single(input_path, output_path, model);
        if (model) rnnoise_model_free(model);
        return ret;
    }

    size_t overlap = align_to_frame(OVERLAP_SAMPLES);

    fprintf(stderr, "Parallel: %d threads, overlap=%zu samples (%.0f ms)\n",
            num_threads, overlap, (double)overlap / SAMPLE_RATE * 1000.0);

    /* Read entire file into memory */
    short *pcm_data = (short *)malloc(sizeof(short) * total_samples);
    if (!pcm_data) {
        fprintf(stderr, "Failed to allocate %zu bytes for input PCM\n",
                sizeof(short) * total_samples);
        if (model) rnnoise_model_free(model);
        return 1;
    }

    fin = fopen(input_path, "rb");
    if (!fin) {
        fprintf(stderr, "Failed to re-open input\n");
        free(pcm_data);
        if (model) rnnoise_model_free(model);
        return 1;
    }
    size_t actually_read = fread(pcm_data, sizeof(short), total_samples, fin);
    fclose(fin);
    if (actually_read < total_samples) {
        total_samples = actually_read;
    }

    /* Calculate segment boundaries */
    size_t base_seg_samples = align_to_frame(total_samples / (size_t)num_threads);

    /* Prepare thread arguments */
    ThreadArg *targs = (ThreadArg *)calloc((size_t)num_threads, sizeof(ThreadArg));
    pthread_t *threads = (pthread_t *)calloc((size_t)num_threads, sizeof(pthread_t));

    /* Track the actual start/end for each segment (without overlap) for reassembly */
    size_t *seg_logical_start = (size_t *)calloc((size_t)num_threads, sizeof(size_t));
    size_t *seg_logical_end = (size_t *)calloc((size_t)num_threads, sizeof(size_t));
    /* Track what the thread's input actually starts at (with overlap) */
    size_t *seg_actual_start = (size_t *)calloc((size_t)num_threads, sizeof(size_t));
    /* Track if segment has left/right overlap */
    int *has_left_overlap = (int *)calloc((size_t)num_threads, sizeof(int));
    int *has_right_overlap = (int *)calloc((size_t)num_threads, sizeof(int));

    if (!targs || !threads || !seg_logical_start || !seg_logical_end ||
        !seg_actual_start || !has_left_overlap || !has_right_overlap) {
        fprintf(stderr, "Failed to allocate thread structures\n");
        free(pcm_data);
        if (model) rnnoise_model_free(model);
        return 1;
    }

    for (int i = 0; i < num_threads; i++) {
        /* Logical segment: the non-overlapping portion this thread "owns" */
        size_t log_start = (size_t)i * base_seg_samples;
        size_t log_end = (i == num_threads - 1) ? total_samples : ((size_t)(i + 1) * base_seg_samples);

        /* Actual input segment: includes overlap on both sides */
        size_t act_start, act_end;

        if (i > 0) {
            act_start = align_to_frame(log_start > overlap ? log_start - overlap : 0);
            has_left_overlap[i] = 1;
        } else {
            act_start = log_start;
            has_left_overlap[i] = 0;
        }

        if (i < num_threads - 1) {
            act_end = log_end + overlap;
            if (act_end > total_samples) act_end = total_samples;
            act_end = align_to_frame(act_end);
            if (act_end <= log_end) act_end = log_end; /* safety */
            has_right_overlap[i] = 1;
        } else {
            act_end = log_end;
            has_right_overlap[i] = 0;
        }

        seg_logical_start[i] = log_start;
        seg_logical_end[i] = log_end;
        seg_actual_start[i] = act_start;

        targs[i].thread_id = i;
        targs[i].input_samples = pcm_data + act_start;
        targs[i].input_num_samples = act_end - act_start;
        targs[i].model = model;
        targs[i].success = 0;

        fprintf(stderr, "  Thread %d: logical [%zu - %zu], actual [%zu - %zu] "
                "(%zu samples, %.1f sec), overlap L=%d R=%d\n",
                i, log_start, log_end, act_start, act_end,
                act_end - act_start, (double)(act_end - act_start) / SAMPLE_RATE,
                has_left_overlap[i], has_right_overlap[i]);
    }

    /* Launch threads */
    for (int i = 0; i < num_threads; i++) {
        int rc = pthread_create(&threads[i], NULL, denoise_segment, &targs[i]);
        if (rc != 0) {
            fprintf(stderr, "Failed to create thread %d (rc=%d)\n", i, rc);
            targs[i].success = 0;
        }
    }

    /* Wait for all threads */
    for (int i = 0; i < num_threads; i++) {
        pthread_join(threads[i], NULL);
    }

    /* Check success */
    for (int i = 0; i < num_threads; i++) {
        if (!targs[i].success) {
            fprintf(stderr, "Thread %d failed!\n", i);
            /* Cleanup */
            for (int j = 0; j < num_threads; j++) {
                free(targs[j].output_samples);
            }
            free(targs);
            free(threads);
            free(seg_logical_start);
            free(seg_logical_end);
            free(seg_actual_start);
            free(has_left_overlap);
            free(has_right_overlap);
            free(pcm_data);
            if (model) rnnoise_model_free(model);
            return 1;
        }
    }

    fprintf(stderr, "All threads complete, reassembling with crossfade...\n");

    /*
     * Reassembly strategy:
     *
     * Each thread's output corresponds to its actual input (minus 1 warmup frame).
     * Because of the warmup frame skip, the output is shifted by FRAME_SIZE samples
     * relative to the input.
     *
     * For thread i, input started at seg_actual_start[i], so the output represents
     * audio starting at seg_actual_start[i] + FRAME_SIZE.
     *
     * For the logical region [log_start, log_end), we need to extract the
     * corresponding portion from each thread's output, with crossfade at boundaries.
     *
     * Output sample at position P in the original stream comes from thread i's
     * output at offset: P - (seg_actual_start[i] + FRAME_SIZE)
     */

    FILE *fout = fopen(output_path, "wb");
    if (!fout) {
        fprintf(stderr, "Failed to open output: %s\n", output_path);
        for (int i = 0; i < num_threads; i++) free(targs[i].output_samples);
        free(targs); free(threads);
        free(seg_logical_start); free(seg_logical_end); free(seg_actual_start);
        free(has_left_overlap); free(has_right_overlap);
        free(pcm_data);
        if (model) rnnoise_model_free(model);
        return 1;
    }

    for (int i = 0; i < num_threads; i++) {
        size_t out_origin = seg_actual_start[i] + FRAME_SIZE; /* warmup offset */
        size_t out_len = targs[i].output_num_samples;

        /*
         * Determine what range of original samples this output covers:
         * [out_origin, out_origin + out_len)
         */

        /* We want to write the logical region [log_start, log_end)
         * but at boundaries we need to crossfade. */

        size_t write_start = seg_logical_start[i];
        size_t write_end = seg_logical_end[i];

        /* For thread 0, the output starts at FRAME_SIZE (after warmup), not at 0.
         * So the first FRAME_SIZE samples of the original stream are lost.
         * This matches the original rnnoise_demo behavior. */
        if (i == 0) {
            write_start = out_origin;
        }

        /* Clamp write_end to what we actually have */
        if (out_origin + out_len < write_end) {
            write_end = out_origin + out_len;
        }

        if (write_start >= write_end) {
            continue;
        }

        /* Offset into this thread's output buffer */
        size_t buf_offset;
        if (write_start >= out_origin) {
            buf_offset = write_start - out_origin;
        } else {
            buf_offset = 0;
            write_start = out_origin;
        }

        size_t samples_to_write = write_end - write_start;

        /* Safety check */
        if (buf_offset + samples_to_write > out_len) {
            samples_to_write = out_len - buf_offset;
        }

        if (has_left_overlap[i] && i > 0) {
            /*
             * Crossfade region: the first 'overlap' samples of this thread's
             * logical region overlap with the last 'overlap' samples of the
             * previous thread's logical region.
             *
             * We need to:
             * 1. Read back the last 'overlap' samples we wrote (from prev thread)
             * 2. Crossfade with the current thread's overlap region
             * 3. Seek back and overwrite
             */
            size_t cf_samples = overlap;

            /* The overlap in this thread's output is at the beginning
             * (because we included overlap samples before the logical start) */
            /* The overlap region in this thread's output is:
             * [logical_start_in_buf - overlap, logical_start_in_buf) 
             * But we need the one starting at logical_start_in_buf (the crossfade zone) */

            /* Actually, let's think about this differently:
             * Previous thread wrote up to seg_logical_end[i-1].
             * This thread's logical region starts at seg_logical_start[i].
             * The overlap region is [seg_logical_start[i], seg_logical_start[i] + overlap).
             *
             * Previous thread's output for this region: the last 'overlap' samples it wrote.
             * Current thread's output for this region: starting at buf_offset.
             */

            if (cf_samples > samples_to_write) cf_samples = samples_to_write;

            /* Get previous thread's data for the overlap region */
            size_t prev_origin = seg_actual_start[i-1] + FRAME_SIZE;
            size_t prev_cf_start = seg_logical_start[i] - prev_origin;
            size_t prev_len = targs[i-1].output_num_samples;

            if (prev_cf_start + cf_samples <= prev_len && buf_offset + cf_samples <= out_len) {
                /* Allocate crossfade buffer */
                short *cf_buf = (short *)malloc(sizeof(short) * cf_samples);
                if (cf_buf) {
                    cosine_crossfade(
                        targs[i-1].output_samples + prev_cf_start,
                        targs[i].output_samples + buf_offset,
                        cf_buf, cf_samples
                    );
                    fwrite(cf_buf, sizeof(short), cf_samples, fout);
                    free(cf_buf);

                    /* Write rest of the segment after the crossfade */
                    if (samples_to_write > cf_samples) {
                        fwrite(targs[i].output_samples + buf_offset + cf_samples,
                               sizeof(short), samples_to_write - cf_samples, fout);
                    }
                } else {
                    /* Fallback: no crossfade */
                    fwrite(targs[i].output_samples + buf_offset,
                           sizeof(short), samples_to_write, fout);
                }
            } else {
                /* Cannot crossfade (bounds issue), just write directly */
                fwrite(targs[i].output_samples + buf_offset,
                       sizeof(short), samples_to_write, fout);
            }
        } else {
            /* No left overlap, write directly */
            fwrite(targs[i].output_samples + buf_offset,
                   sizeof(short), samples_to_write, fout);
        }
    }

    fclose(fout);

    /* Get output file size for logging */
    FILE *fcheck = fopen(output_path, "rb");
    if (fcheck) {
        fseek(fcheck, 0, SEEK_END);
        long out_size = ftell(fcheck);
        fclose(fcheck);
        fprintf(stderr, "Output: %ld bytes (%.1f seconds)\n",
                out_size, (double)(out_size / (long)sizeof(short)) / SAMPLE_RATE);
    }

    /* Cleanup */
    for (int i = 0; i < num_threads; i++) {
        free(targs[i].output_samples);
    }
    free(targs);
    free(threads);
    free(seg_logical_start);
    free(seg_logical_end);
    free(seg_actual_start);
    free(has_left_overlap);
    free(has_right_overlap);
    free(pcm_data);
    if (model) rnnoise_model_free(model);

    fprintf(stderr, "Done (parallel, %d threads)\n", num_threads);
    return 0;
}
