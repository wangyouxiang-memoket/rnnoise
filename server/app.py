import asyncio
import os
import tempfile
from typing import Optional

import boto3
from botocore.exceptions import ClientError
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel
from pydub import AudioSegment

APP = FastAPI(title="rnnoise-http")

MAX_CONCURRENCY = int(os.getenv("MAX_CONCURRENCY", "4"))
RNNOISE_BIN = os.getenv("RNNOISE_BIN", "/opt/rnnoise/bin/rnnoise_wrapper_demo")
DEFAULT_MODEL = os.getenv("RNNOISE_MODEL", "/opt/rnnoise/models/weights_blob.bin")
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")

_semaphore = asyncio.Semaphore(MAX_CONCURRENCY)
_active_requests = 0
_s3_client = boto3.client("s3", region_name=AWS_REGION)

# Supported audio formats
SUPPORTED_FORMATS = {
    'aac': 'audio/aac',
    'mp3': 'audio/mpeg',
    'm4a': 'audio/mp4',
    'wav': 'audio/wav',
    'ogg': 'audio/ogg',
    'flac': 'audio/flac',
}

EXTENSION_TO_FORMAT = {
    '.aac': 'aac',
    '.mp3': 'mp3',
    '.m4a': 'm4a',
    '.wav': 'wav',
    '.ogg': 'ogg',
    '.flac': 'flac',
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
    args = [RNNOISE_BIN, input_path, output_path]
    if model_path:
        args.append(model_path)
    result = os.spawnvp(os.P_WAIT, args[0], args)
    if result != 0:
        raise RuntimeError(f"rnnoise failed with code {result}")


def _detect_audio_format(file_path: str) -> Optional[str]:
    """Detect audio format from file extension"""
    _, ext = os.path.splitext(file_path.lower())
    return EXTENSION_TO_FORMAT.get(ext)


def _convert_to_pcm(input_path: str, output_path: str, audio_format: str) -> tuple:
    """Convert audio file to PCM format for rnnoise processing
    Returns: (sample_rate, channels)
    """
    audio = AudioSegment.from_file(input_path, format=audio_format)
    
    # Export as raw PCM: 16-bit, mono, 48kHz (rnnoise typical settings)
    sample_rate = 48000
    channels = 1
    
    # Convert to mono and set sample rate
    audio = audio.set_frame_rate(sample_rate).set_channels(channels)
    
    # Export as raw PCM
    audio.export(output_path, format="s16le", parameters=["-f", "s16le"])
    
    return sample_rate, channels


def _convert_from_pcm(input_pcm_path: str, output_path: str, target_format: str, 
                     sample_rate: int = 48000, channels: int = 1) -> None:
    """Convert PCM back to target audio format"""
    # Read raw PCM data
    audio = AudioSegment.from_file(
        input_pcm_path,
        format="s16le",
        frame_rate=sample_rate,
        channels=channels,
        sample_width=2  # 16-bit = 2 bytes
    )
    
    # Export to target format
    audio.export(output_path, format=target_format)


@APP.get("/health")
async def health() -> dict:
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
    
    if _active_requests >= MAX_CONCURRENCY:
        raise HTTPException(
            status_code=503,
            detail=f"Service busy: {_active_requests}/{MAX_CONCURRENCY} slots in use"
        )
    
    content_type = request.headers.get("content-type", "")
    if "application/octet-stream" not in content_type and "audio/" not in content_type:
        raise HTTPException(
            status_code=415, 
            detail="Use application/octet-stream or audio/* content type"
        )

    model_param = request.query_params.get("model")
    model_path = model_param if model_param else (DEFAULT_MODEL if os.path.exists(DEFAULT_MODEL) else None)
    
    # Get format from query parameter
    audio_format = request.query_params.get("format", "").lower()
    
    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="Empty body")

    async with _semaphore:
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
                    sample_rate, channels = await loop.run_in_executor(
                        None, _convert_to_pcm, original_file, input_pcm, audio_format
                    )
                    
                    # Process with rnnoise
                    await loop.run_in_executor(None, _run_rnnoise, input_pcm, output_pcm, model_path)
                    
                    # Convert back to original format
                    await loop.run_in_executor(
                        None, _convert_from_pcm, output_pcm, output_file, audio_format, sample_rate, channels
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

                    await loop.run_in_executor(None, _run_rnnoise, input_path, output_path, model_path)

                    with open(output_path, "rb") as output_file:
                        output_bytes = output_file.read()
                    
                    media_type = "application/octet-stream"

            return Response(content=output_bytes, media_type=media_type)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        finally:
            _active_requests -= 1


@APP.post("/denoise/s3")
async def denoise_s3(req: DenoiseRequest) -> dict:
    """S3-based denoise: read from S3, process, write back to S3
    Automatically detects format from file extension and preserves it.
    """
    global _active_requests
    
    print(f"[S3] Received request: {req.input_s3_uri} -> {req.output_s3_uri}")
    
    if _active_requests >= MAX_CONCURRENCY:
        raise HTTPException(
            status_code=503,
            detail=f"Service busy: {_active_requests}/{MAX_CONCURRENCY} slots in use"
        )
    
    try:
        input_bucket, input_key = _parse_s3_uri(req.input_s3_uri)
        output_bucket, output_key = _parse_s3_uri(req.output_s3_uri)
        print(f"[S3] Parsed - Bucket: {input_bucket}, Key: {input_key}")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    async with _semaphore:
        _active_requests += 1
        try:
            loop = asyncio.get_running_loop()
            with tempfile.TemporaryDirectory() as temp_dir:
                # Detect format from input file extension
                audio_format = _detect_audio_format(input_key)
                print(f"[S3] Detected format: {audio_format}")
                
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
                    print(f"[S3] Downloading from S3: {input_bucket}/{input_key}")
                    await loop.run_in_executor(
                        None, _s3_client.download_file, input_bucket, input_key, input_file
                    )
                    print(f"[S3] Download complete")

                    # Download model if specified
                    if req.model_s3_uri:
                        model_bucket, model_key = _parse_s3_uri(req.model_s3_uri)
                        model_path = os.path.join(temp_dir, "model.bin")
                        await loop.run_in_executor(
                            None, _s3_client.download_file, model_bucket, model_key, model_path
                        )
                    elif os.path.exists(DEFAULT_MODEL):
                    # Convert to PCM if needed
                    if audio_format:
                        print(f"[S3] Converting {audio_format} to PCM")
                        sample_rate, channels = await loop.run_in_executor(
                            None, _convert_to_pcm, input_file, input_pcm, audio_format
                        )
                        print(f"[S3] Conversion complete: {sample_rate}Hz, {channels}ch")
                    
                    # Process with rnnoise
                    print(f"[S3] Processing with rnnoise")
                    await loop.run_in_executor(None, _run_rnnoise, input_pcm, output_pcm, model_path)
                    # Convert back to original format if needed
                    if audio_format:
                        print(f"[S3] Converting PCM back to {audio_format}")
                        await loop.run_in_executor(
                            None, _convert_from_pcm, output_pcm, output_file, audio_format, sample_rate, channels
                        )
                        print(f"[S3] Conversion back complete")
                    
                    # Upload to S3
                    print(f"[S3] Uploading to S3: {output_bucket}/{output_key}")
                    await loop.run_in_executor(
                        None, _s3_client.upload_file, output_file, output_bucket, output_key
                    )
                    print(f"[S3] Upload complete")
                    # Upload to S3
                    await loop.run_in_executor(
                        None, _s3_client.upload_file, output_file, output_bucket, output_key
                    )

                except ClientError as exc:
                    print(f"[S3] S3 Error: {exc}")
                    raise HTTPException(status_code=500, detail=f"S3 error: {exc}") from exc
                except Exception as exc:
                    print(f"[S3] Error: {exc}")
                    import traceback
                    traceback.print_exc()
                    raise HTTPException(status_code=500, detail=str(exc)) from exc

            return {
                "status": "success",
                "input": req.input_s3_uri,
                "output": req.output_s3_uri,
                "format": audio_format if audio_format else "pcm",
            }
        finally:
            _active_requests -= 1
