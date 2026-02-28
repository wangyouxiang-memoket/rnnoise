# RNNoise API - 多格式音频支持

API现在支持以下音频格式的降噪处理：
- AAC (.aac)
- MP3 (.mp3)
- M4A (.m4a)
- WAV (.wav)
- OGG (.ogg)
- FLAC (.flac)

## 使用方法

### 1. 直接上传接口 `/denoise`

#### 处理音频文件（支持多种格式）

```bash
curl -X POST "http://localhost:8080/denoise?format=mp3" \
  -H "Content-Type: application/octet-stream" \
  --data-binary @input.mp3 \
  -o output.mp3
```

#### 参数说明：
- `format`: 音频格式（aac, mp3, m4a, wav, ogg, flac）
  - 如果指定格式，API会自动解码、降噪并以原格式返回
  - 如果不指定格式，则按照原始PCM处理
- `model`: 可选的模型文件路径

#### 示例：

处理AAC文件：
```bash
curl -X POST "http://localhost:8080/denoise?format=aac" \
  -H "Content-Type: application/octet-stream" \
  --data-binary @voice.aac \
  -o voice_denoised.aac
```

处理WAV文件：
```bash
curl -X POST "http://localhost:8080/denoise?format=wav" \
  -H "Content-Type: application/octet-stream" \
  --data-binary @recording.wav \
  -o recording_denoised.wav
```

处理FLAC文件：
```bash
curl -X POST "http://localhost:8080/denoise?format=flac" \
  -H "Content-Type: application/octet-stream" \
  --data-binary @audio.flac \
  -o audio_denoised.flac
```

### 2. S3接口 `/denoise/s3`

S3接口会自动从文件扩展名检测格式，并保持原格式输出。

```bash
curl -X POST "http://localhost:8080/denoise/s3" \
  -H "Content-Type: application/json" \
  -d '{
    "input_s3_uri": "s3://my-bucket/input/audio.mp3",
    "output_s3_uri": "s3://my-bucket/output/audio_denoised.mp3"
  }'
```

#### 响应示例：
```json
{
  "status": "success",
  "input": "s3://my-bucket/input/audio.mp3",
  "output": "s3://my-bucket/output/audio_denoised.mp3",
  "format": "mp3"
}
```

## 技术细节

### 处理流程：
1. 接收音频文件（任意支持的格式）
2. 自动转换为PCM格式（48kHz, 单声道, 16-bit）
3. 使用RNNoise进行降噪处理
4. 转换回原始格式
5. 返回处理后的音频文件

### 音频参数：
- 采样率：48kHz
- 声道：单声道（mono）
- 位深：16-bit

## 部署说明

确保Docker镜像包含FFmpeg支持（已在Dockerfile中添加）：

```dockerfile
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    ffmpeg \
  && rm -rf /var/lib/apt/lists/*
```

## 依赖项

所需Python包（已在requirements.txt中）：
- fastapi==0.115.7
- uvicorn==0.27.1
- boto3==1.35.76
- pydub==0.25.1

系统依赖：
- FFmpeg（用于音频格式转换）
