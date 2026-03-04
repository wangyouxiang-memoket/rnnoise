import asyncio
import os
import tempfile
from typing import Optional
import logging
import time

import boto3
from botocore.exceptions import ClientError
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel
from pydub import AudioSegment

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

APP = FastAPI(title="rnnoise-http")

logger.info("=" * 50)
logger.info("RNNoise Server Starting...")
logger.info(f"MAX_CONCURRENCY: {os.getenv('MAX_CONCURRENCY', '4')}")
logger.info(f"RNNOISE_BIN: {os.getenv('RNNOISE_BIN', '/opt/rnnoise/bin/rnnoise_parallel_demo')}")
logger.info(f"RNNOISE_MAX_THREADS: {os.getenv('RNNOISE_MAX_THREADS', 'auto (cpu_count)')}")
logger.info(f"AWS_REGION: {os.getenv('AWS_REGION', 'us-east-1')}")
logger.info("=" * 50)

MAX_CONCURRENCY = int(os.getenv("MAX_CONCURRENCY", "4"))
RNNOISE_BIN = os.getenv("RNNOISE_BIN", "/opt/rnnoise/bin/rnnoise_parallel_demo")
DEFAULT_MODEL = os.getenv("RNNOISE_MODEL", "/opt/rnnoise/models/weights_blob.bin")
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")

# Global thread budget: max total rnnoise threads across all concurrent requests
# This prevents CPU oversubscription when multiple large files are processed simultaneously
import threading as _threading
RNNOISE_MAX_TOTAL_THREADS = int(os.getenv("RNNOISE_MAX_THREADS", str(os.cpu_count() or 4)))
_rnnoise_thread_budget = RNNOISE_MAX_TOTAL_THREADS
_rnnoise_budget_lock = _threading.Lock()

_semaphore = asyncio.Semaphore(MAX_CONCURRENCY)
_active_requests = 0
# Max time (seconds) a request will wait in queue before being rejected
QUEUE_TIMEOUT = int(os.getenv("QUEUE_TIMEOUT", "300"))  # 5 minutes
_s3_client = boto3.client("s3", region_name=AWS_REGION)

# Supported audio formats
SUPPORTED_FORMATS = {
    "aac": "audio/aac",
    "mp3": "audio/mpeg",
    "m4a": "audio/mp4",
    "wav": "audio/wav",
    "ogg": "audio/ogg",
    "flac": "audio/flac",
}

EXTENSION_TO_FORMAT = {
    ".aac": "aac",
    ".mp3": "mp3",
    ".m4a": "m4a",
    ".wav": "wav",
    ".ogg": "ogg",
    ".flac": "flac",
}


class DenoiseRequest(BaseModel):
    input_s3_uri: str
    output_s3_uri: str
    model_s3_uri: Optional[str] = None


def _parse_s3_uri(s3_uri: str) -> tuple:
    if not s3_uri.startswith("s3://"):
        raise ValueError(f"Invalid S3 URI: {s3_uri}")
    parts = s3_uri[5:].split("/", 1)
    if len(parts) != 2:
        raise ValueError(f"Invalid S3 URI format: {s3_uri}")
    return parts[0], parts[1]


def _run_rnnoise(input_path: str, output_path: str, model_path: Optional[str]) -> None:
    """Run rnnoise with progress logging for long files.
    Dynamically allocates parallel threads from a global budget to prevent
    CPU oversubscription when multiple requests run concurrently."""
    global _rnnoise_thread_budget
    import os
    
    # Get file size to estimate duration
    file_size = os.path.getsize(input_path)
    # 48kHz, mono, 16-bit = 96,000 bytes/sec
    estimated_duration = file_size / 96000
    
    if estimated_duration > 60:  # Log for files > 1 minute
        logger.info(f"Processing large file (~{estimated_duration/60:.1f} min), this may take a while...")
    
    # Allocate threads from global budget
    # Short files (<5min) always get 1 thread (C program handles this internally)
    # Long files get up to 4 threads, limited by available budget
    allocated_threads = 1
    need_budget = False
    if estimated_duration >= 300:  # >= 5 minutes, eligible for parallel
        need_budget = True
        with _rnnoise_budget_lock:
            available = _rnnoise_thread_budget
            if available >= 2:
                # Allocate up to 4 threads, but reserve at least 1 for other requests
                allocated_threads = min(4, available - 1)
                _rnnoise_thread_budget -= allocated_threads
            else:
                # Budget exhausted or nearly so: this request gets 1 thread (single-threaded)
                # Don't deduct from budget since 1 thread is the baseline
                allocated_threads = 1
                need_budget = False  # nothing extra to return later
            logger.info(f"Thread budget: allocated {allocated_threads}, remaining {_rnnoise_thread_budget}/{RNNOISE_MAX_TOTAL_THREADS}")
    
    try:
        args = [RNNOISE_BIN, input_path, output_path]
        if model_path:
            args.append(model_path)
        else:
            args.append("")  # placeholder for model_path since max_threads is argv[4]
        args.append(str(allocated_threads))
        result = os.spawnvp(os.P_WAIT, args[0], args)
        if result != 0:
            raise RuntimeError(f"rnnoise failed with code {result}")
    finally:
        # Return threads to the budget
        if need_budget:
            with _rnnoise_budget_lock:
                _rnnoise_thread_budget += allocated_threads
                logger.info(f"Thread budget: returned {allocated_threads}, now {_rnnoise_thread_budget}/{RNNOISE_MAX_TOTAL_THREADS}")


def _detect_audio_format(file_path: str) -> Optional[str]:
    """Detect audio format from file extension"""
    _, ext = os.path.splitext(file_path.lower())
    return EXTENSION_TO_FORMAT.get(ext)


def _convert_to_pcm(input_path: str, output_path: str, audio_format: str) -> tuple:
    """Convert audio file to PCM format for rnnoise processing
    Returns: (original_sample_rate, original_channels, rnnoise_sample_rate, rnnoise_channels)
    """
    start_time = time.time()
    
    try:
        # Try to load with explicit format first
        audio = AudioSegment.from_file(input_path, format=audio_format)
    except Exception as e:
        logger.warning(f"Failed to load with format={audio_format}, trying auto-detect: {e}")
        # Fallback: let ffmpeg auto-detect
        audio = AudioSegment.from_file(input_path)
    
    # Store original parameters
    original_sample_rate = audio.frame_rate
    original_channels = audio.channels
    
    logger.info(f"Original audio: {original_sample_rate}Hz, {original_channels}ch, duration={len(audio)}ms")

    # RNNoise is hardcoded for 48kHz (FRAME_SIZE=480 = 10ms at 48kHz)
    # Always use 48kHz for optimal performance and quality
    # The resampling overhead (1-4s) is negligible compared to denoise time
    rnnoise_sample_rate = 48000
    rnnoise_channels = 1  # Always convert to mono for denoising
    
    if original_sample_rate != 48000:
        logger.info(f"Resampling {original_sample_rate}Hz → 48000Hz (RNNoise requirement)")
    if original_channels > 1:
        logger.info(f"Converting {original_channels}ch → mono for processing")

    # Convert to mono and set sample rate
    audio = audio.set_channels(rnnoise_channels).set_frame_rate(rnnoise_sample_rate)

    # Export as raw PCM (s16le format)
    # Use pydub export to ensure correct format
    audio.export(
        output_path,
        format="s16le",
        codec="pcm_s16le",
        parameters=["-f", "s16le", "-acodec", "pcm_s16le"]
    )
    
    # Verify output
    import os
    pcm_size = os.path.getsize(output_path)
    expected_size = len(audio.raw_data)
    logger.info(f"PCM export: {pcm_size} bytes (expected ~{expected_size})")
    
    elapsed = time.time() - start_time
    logger.info(f"Conversion to PCM: {rnnoise_sample_rate}Hz, {rnnoise_channels}ch (took {elapsed:.2f}s)")

    return original_sample_rate, original_channels, rnnoise_sample_rate, rnnoise_channels


def _convert_from_pcm(
    input_pcm_path: str,
    output_path: str,
    target_format: str,
    processing_sample_rate: int,
    processing_channels: int,
    original_sample_rate: int,
    original_channels: int,
) -> None:
    """Convert PCM back to target audio format, restoring original parameters"""
    start_time = time.time()
    logger.info(f"Reading processed PCM: {processing_sample_rate}Hz, {processing_channels}ch")
    
    # Verify PCM file exists and has data
    import os
    if not os.path.exists(input_pcm_path):
        raise RuntimeError(f"Processed PCM file not found: {input_pcm_path}")
    
    pcm_size = os.path.getsize(input_pcm_path)
    if pcm_size == 0:
        raise RuntimeError(f"Processed PCM file is empty")
    
    logger.info(f"PCM file size: {pcm_size} bytes")
    
    # Read raw PCM data - use from_raw for better control
    with open(input_pcm_path, 'rb') as f:
        pcm_data = f.read()
    
    audio = AudioSegment(
        data=pcm_data,
        sample_width=2,  # 16-bit = 2 bytes
        frame_rate=processing_sample_rate,
        channels=processing_channels
    )
    
    # Restore original sample rate and channels if different
    if processing_sample_rate != original_sample_rate:
        audio = audio.set_frame_rate(original_sample_rate)
        logger.info(f"Restored sample rate to {original_sample_rate}Hz")
    
    # Note: Keeping mono since denoising was done in mono
    # Converting back to stereo from mono wouldn't add information
    
    logger.info(f"Exporting to {target_format}")

    # Export to target format with proper codec settings and quality
    if target_format == "m4a":
        # M4A with AAC codec - use better quality settings
        audio.export(
            output_path, 
            format="ipod",
            codec="aac",
            bitrate="128k",
            parameters=["-strict", "experimental"]
        )
    elif target_format == "mp3":
        # MP3 with good quality
        audio.export(output_path, format="mp3", bitrate="128k", parameters=["-q:a", "2"])
    elif target_format == "ogg":
        audio.export(output_path, format="ogg", codec="libvorbis", parameters=["-q:a", "5"])
    elif target_format == "flac":
        # FLAC is lossless
        audio.export(output_path, format="flac")
    elif target_format == "wav":
        audio.export(output_path, format="wav")
    elif target_format == "aac":
        audio.export(output_path, format="adts", codec="aac", bitrate="128k")
    else:
        audio.export(output_path, format=target_format)
    
    elapsed = time.time() - start_time
    logger.info(f"Conversion from PCM complete (took {elapsed:.2f}s)")


@APP.get("/health")
async def health() -> dict:
    logger.info("Health check called")
    return {
        "status": "ok",
        "active_requests": _active_requests,
        "max_concurrency": MAX_CONCURRENCY,
    }


@APP.post("/denoise")
async def denoise_direct(request: Request) -> Response:
    """Direct binary upload/download with multi-format support

    Query parameters:
    - model: path to model file (optional)
    - format: audio format (aac, mp3, m4a, wav, ogg, flac) - if not specified, treats as raw PCM
    """
    global _active_requests

    # Wait in queue for a slot (with timeout), instead of rejecting immediately
    try:
        await asyncio.wait_for(_semaphore.acquire(), timeout=QUEUE_TIMEOUT)
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=503,
            detail=f"Queue timeout: waited {QUEUE_TIMEOUT}s, {_active_requests}/{MAX_CONCURRENCY} slots in use",
        )

    content_type = request.headers.get("content-type", "")
    if "application/octet-stream" not in content_type and "audio/" not in content_type:
        raise HTTPException(
            status_code=415,
            detail="Use application/octet-stream or audio/* content type",
        )

    model_param = request.query_params.get("model")
    model_path = (
        model_param
        if model_param
        else (DEFAULT_MODEL if os.path.exists(DEFAULT_MODEL) else None)
    )

    # Get format from query parameter
    audio_format = request.query_params.get("format", "").lower()

    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="Empty body")

    # Semaphore already acquired via wait_for above
    _active_requests += 1
    try:
        loop = asyncio.get_running_loop()
        with tempfile.TemporaryDirectory() as temp_dir:
            # Check if we need format conversion
            if audio_format and audio_format in SUPPORTED_FORMATS:
                # Multi-format workflow
                original_file = os.path.join(temp_dir, f"input.{audio_format}")
                input_pcm = os.path.join(temp_dir, "input.pcm")
                output_pcm = os.path.join(temp_dir, "output.pcm")
                output_file = os.path.join(temp_dir, f"output.{audio_format}")

                # Write original audio file
                with open(original_file, "wb") as f:
                    f.write(body)

                # Convert to PCM
                orig_sr, orig_ch, proc_sr, proc_ch = await loop.run_in_executor(
                    None, _convert_to_pcm, original_file, input_pcm, audio_format
                )

                # Process with rnnoise
                await loop.run_in_executor(
                    None, _run_rnnoise, input_pcm, output_pcm, model_path
                )

                # Convert back to original format
                await loop.run_in_executor(
                    None,
                    _convert_from_pcm,
                    output_pcm,
                    output_file,
                    audio_format,
                    proc_sr,
                    proc_ch,
                    orig_sr,
                    orig_ch,
                )

                # Read output file
                with open(output_file, "rb") as f:
                    output_bytes = f.read()

                media_type = SUPPORTED_FORMATS[audio_format]
            else:
                # Legacy PCM workflow
                input_path = os.path.join(temp_dir, "input.pcm")
                output_path = os.path.join(temp_dir, "output.pcm")

                with open(input_path, "wb") as input_file:
                    input_file.write(body)

                await loop.run_in_executor(
                    None, _run_rnnoise, input_path, output_path, model_path
                )

                with open(output_path, "rb") as output_file:
                    output_bytes = output_file.read()

                media_type = "application/octet-stream"

        return Response(content=output_bytes, media_type=media_type)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        _active_requests -= 1
        _semaphore.release()


@APP.post("/denoise/s3")
async def denoise_s3(req: DenoiseRequest) -> dict:
    """S3-based denoise: read from S3, process, write back to S3
    Automatically detects format from file extension and preserves it.
    """
    request_start = time.time()
    logger.info("===== NEW S3 REQUEST RECEIVED =====")
    logger.info(f"Input: {req.input_s3_uri}")
    logger.info(f"Output: {req.output_s3_uri}")
    global _active_requests

    # Wait in queue for a slot (with timeout), instead of rejecting immediately
    try:
        await asyncio.wait_for(_semaphore.acquire(), timeout=QUEUE_TIMEOUT)
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=503,
            detail=f"Queue timeout: waited {QUEUE_TIMEOUT}s, {_active_requests}/{MAX_CONCURRENCY} slots in use",
        )

    try:
        input_bucket, input_key = _parse_s3_uri(req.input_s3_uri)
        output_bucket, output_key = _parse_s3_uri(req.output_s3_uri)
        logger.info(f"Parsed S3 - Bucket: {input_bucket}, Key: {input_key}")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Semaphore already acquired via wait_for above
    _active_requests += 1
    try:
        loop = asyncio.get_running_loop()
        with tempfile.TemporaryDirectory() as temp_dir:
            # Detect format from input file extension
            audio_format = _detect_audio_format(input_key)
            logger.info(f"Detected format: {audio_format}")

            if audio_format:
                # Multi-format workflow
                input_file = os.path.join(temp_dir, f"input.{audio_format}")
                input_pcm = os.path.join(temp_dir, "input.pcm")
                output_pcm = os.path.join(temp_dir, "output.pcm")
                output_file = os.path.join(temp_dir, f"output.{audio_format}")
            else:
                # Legacy PCM workflow
                input_file = os.path.join(temp_dir, "input.pcm")
                output_file = os.path.join(temp_dir, "output.pcm")
                input_pcm = input_file
                output_pcm = output_file

            model_path = None

            try:
                # Download input from S3
                download_start = time.time()
                logger.info(f"Downloading from S3: {input_bucket}/{input_key}")
                await loop.run_in_executor(
                    None,
                    _s3_client.download_file,
                    input_bucket,
                    input_key,
                    input_file,
                )
                logger.info(f"Download complete (took {time.time() - download_start:.2f}s)")

                # Download model if specified
                if req.model_s3_uri:
                    model_bucket, model_key = _parse_s3_uri(req.model_s3_uri)
                    model_path = os.path.join(temp_dir, "model.bin")
                    await loop.run_in_executor(
                        None,
                        _s3_client.download_file,
                        model_bucket,
                        model_key,
                        model_path,
                    )
                elif os.path.exists(DEFAULT_MODEL):
                    model_path = DEFAULT_MODEL

                # Convert to PCM if needed
                if audio_format:
                    logger.info(f"Converting {audio_format} to PCM...")
                    orig_sr, orig_ch, proc_sr, proc_ch = await loop.run_in_executor(
                        None, _convert_to_pcm, input_file, input_pcm, audio_format
                    )

                # Process with rnnoise
                denoise_start = time.time()
                logger.info(f"Starting RNNoise processing...")
                await loop.run_in_executor(
                    None, _run_rnnoise, input_pcm, output_pcm, model_path
                )
                logger.info(f"RNNoise processing complete (took {time.time() - denoise_start:.2f}s)")

                # Convert back to original format if needed
                if audio_format:
                    logger.info(f"Converting PCM back to {audio_format}...")
                    await loop.run_in_executor(
                        None,
                        _convert_from_pcm,
                        output_pcm,
                        output_file,
                        audio_format,
                        proc_sr,
                        proc_ch,
                        orig_sr,
                        orig_ch,
                    )

                # Upload to S3
                upload_start = time.time()
                logger.info(f"Uploading to S3: {output_bucket}/{output_key}")
                await loop.run_in_executor(
                    None,
                    _s3_client.upload_file,
                    output_file,
                    output_bucket,
                    output_key,
                )
                logger.info(f"Upload complete (took {time.time() - upload_start:.2f}s)")

            except ClientError as exc:
                logger.error(f"S3 Error: {exc}")
                raise HTTPException(
                    status_code=500, detail=f"S3 error: {exc}"
                ) from exc
            except Exception as exc:
                logger.error(f"Error during processing: {exc}", exc_info=True)
                raise HTTPException(status_code=500, detail=str(exc)) from exc

        total_time = time.time() - request_start
        logger.info(f"===== REQUEST COMPLETE (total time: {total_time:.2f}s) =====")
        return {
            "status": "success",
            "input": req.input_s3_uri,
            "output": req.output_s3_uri,
            "format": audio_format if audio_format else "pcm",
            "processing_time": round(total_time, 2),
        }
    finally:
        _active_requests -= 1
        _semaphore.release()