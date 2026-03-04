#!/usr/bin/env python3
"""
Test audio conversion functionality
"""
import os
import sys
import tempfile
import subprocess
from pathlib import Path

# Add server to path
sys.path.insert(0, os.path.dirname(__file__))

from pydub import AudioSegment
from pydub.generators import Sine


def create_test_audio(format_name, duration_ms=5000, sample_rate=44100):
    """Create a test audio file in various formats"""
    print(f"\n{'='*60}")
    print(f"Creating test {format_name} file...")
    
    # Generate a simple sine wave (440Hz = A4 note)
    audio = Sine(440).to_audio_segment(duration=duration_ms)
    audio = audio.set_frame_rate(sample_rate).set_channels(2)  # Stereo
    
    with tempfile.NamedTemporaryFile(suffix=f'.{format_name}', delete=False) as f:
        temp_path = f.name
    
    # Export with appropriate settings
    if format_name == 'm4a':
        audio.export(temp_path, format='ipod', codec='aac', bitrate='128k')
    elif format_name == 'mp3':
        audio.export(temp_path, format='mp3', bitrate='128k')
    elif format_name == 'ogg':
        audio.export(temp_path, format='ogg', codec='libvorbis')
    elif format_name == 'flac':
        audio.export(temp_path, format='flac')
    elif format_name == 'wav':
        audio.export(temp_path, format='wav')
    elif format_name == 'aac':
        audio.export(temp_path, format='adts', codec='aac', bitrate='128k')
    else:
        raise ValueError(f"Unsupported format: {format_name}")
    
    size = os.path.getsize(temp_path)
    print(f"✓ Created {format_name}: {temp_path} ({size} bytes)")
    return temp_path


def test_conversion_to_pcm(input_file, format_name):
    """Test converting audio to PCM"""
    print(f"\n--- Testing {format_name} → PCM conversion ---")
    
    try:
        # Load audio
        audio = AudioSegment.from_file(input_file, format=format_name)
        print(f"✓ Loaded {format_name}: {audio.frame_rate}Hz, {audio.channels}ch, {len(audio)}ms")
        
        # Convert to 48kHz mono (like our app does)
        audio = audio.set_channels(1).set_frame_rate(48000)
        print(f"✓ Converted to: 48000Hz, 1ch")
        
        # Export to PCM
        with tempfile.NamedTemporaryFile(suffix='.pcm', delete=False) as f:
            pcm_path = f.name
        
        audio.export(
            pcm_path,
            format="s16le",
            codec="pcm_s16le",
            parameters=["-f", "s16le", "-acodec", "pcm_s16le"]
        )
        
        pcm_size = os.path.getsize(pcm_path)
        expected_size = len(audio.raw_data)
        print(f"✓ PCM exported: {pcm_size} bytes (expected ~{expected_size})")
        
        # Verify PCM can be read back
        with open(pcm_path, 'rb') as f:
            pcm_data = f.read()
        
        audio_back = AudioSegment(
            data=pcm_data,
            sample_width=2,
            frame_rate=48000,
            channels=1
        )
        print(f"✓ PCM read back: {len(audio_back)}ms")
        
        # Cleanup
        os.unlink(pcm_path)
        
        return True
        
    except Exception as e:
        print(f"✗ FAILED: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_conversion_from_pcm(format_name):
    """Test converting PCM back to target format"""
    print(f"\n--- Testing PCM → {format_name} conversion ---")
    
    try:
        # Create test PCM data (1 second of sine wave)
        audio = Sine(440).to_audio_segment(duration=1000)
        audio = audio.set_frame_rate(48000).set_channels(1)
        
        # Write as PCM
        with tempfile.NamedTemporaryFile(suffix='.pcm', delete=False) as f:
            pcm_path = f.name
        
        pcm_data = audio.raw_data
        with open(pcm_path, 'wb') as f:
            f.write(pcm_data)
        
        print(f"✓ Created test PCM: {len(pcm_data)} bytes")
        
        # Read back from PCM
        with open(pcm_path, 'rb') as f:
            pcm_data_read = f.read()
        
        audio_from_pcm = AudioSegment(
            data=pcm_data_read,
            sample_width=2,
            frame_rate=48000,
            channels=1
        )
        print(f"✓ PCM loaded: {len(audio_from_pcm)}ms")
        
        # Export to target format
        with tempfile.NamedTemporaryFile(suffix=f'.{format_name}', delete=False) as f:
            output_path = f.name
        
        if format_name == 'm4a':
            audio_from_pcm.export(output_path, format='ipod', codec='aac', bitrate='128k', parameters=["-strict", "experimental"])
        elif format_name == 'mp3':
            audio_from_pcm.export(output_path, format='mp3', bitrate='128k', parameters=["-q:a", "2"])
        elif format_name == 'ogg':
            audio_from_pcm.export(output_path, format='ogg', codec='libvorbis', parameters=["-q:a", "5"])
        elif format_name == 'flac':
            audio_from_pcm.export(output_path, format='flac')
        elif format_name == 'wav':
            audio_from_pcm.export(output_path, format='wav')
        elif format_name == 'aac':
            audio_from_pcm.export(output_path, format='adts', codec='aac', bitrate='128k')
        
        output_size = os.path.getsize(output_path)
        print(f"✓ Exported {format_name}: {output_size} bytes")
        
        # Verify it can be read back
        verify_audio = AudioSegment.from_file(output_path)
        print(f"✓ Verified {format_name}: {verify_audio.frame_rate}Hz, {verify_audio.channels}ch, {len(verify_audio)}ms")
        
        # Cleanup
        os.unlink(pcm_path)
        os.unlink(output_path)
        
        return True
        
    except Exception as e:
        print(f"✗ FAILED: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_rnnoise_binary():
    """Test if rnnoise binary works"""
    print(f"\n{'='*60}")
    print("Testing RNNoise binary...")
    
    rnnoise_bin = os.getenv("RNNOISE_BIN", "/opt/rnnoise/bin/rnnoise_wrapper_demo")
    
    if not os.path.exists(rnnoise_bin):
        print(f"✗ RNNoise binary not found: {rnnoise_bin}")
        return False
    
    print(f"✓ RNNoise binary found: {rnnoise_bin}")
    
    try:
        # Create test PCM (1 second, 48kHz mono)
        audio = Sine(440).to_audio_segment(duration=1000)
        audio = audio.set_frame_rate(48000).set_channels(1)
        
        with tempfile.NamedTemporaryFile(suffix='.pcm', delete=False) as f:
            input_pcm = f.name
        with tempfile.NamedTemporaryFile(suffix='.pcm', delete=False) as f:
            output_pcm = f.name
        
        with open(input_pcm, 'wb') as f:
            f.write(audio.raw_data)
        
        input_size = os.path.getsize(input_pcm)
        print(f"✓ Test PCM created: {input_size} bytes")
        
        # Run rnnoise
        result = subprocess.run(
            [rnnoise_bin, input_pcm, output_pcm],
            capture_output=True,
            timeout=10
        )
        
        if result.returncode != 0:
            print(f"✗ RNNoise failed with code {result.returncode}")
            print(f"STDERR: {result.stderr.decode()}")
            return False
        
        output_size = os.path.getsize(output_pcm)
        print(f"✓ RNNoise processed: {output_size} bytes")
        
        if output_size == 0:
            print(f"✗ Output PCM is empty!")
            return False
        
        # Cleanup
        os.unlink(input_pcm)
        os.unlink(output_pcm)
        
        print(f"✓ RNNoise binary works!")
        return True
        
    except Exception as e:
        print(f"✗ RNNoise test failed: {e}")
        import traceback
        traceback.print_exc()
        return False


def main():
    """Run all tests"""
    print("\n" + "="*60)
    print("AUDIO CONVERSION TESTS")
    print("="*60)
    
    formats = ['mp3', 'm4a', 'wav', 'ogg', 'flac', 'aac']
    results = {}
    
    # Test 1: RNNoise binary
    results['rnnoise_binary'] = test_rnnoise_binary()
    
    # Test 2: Format conversions
    for fmt in formats:
        print(f"\n{'='*60}")
        print(f"Testing format: {fmt.upper()}")
        print("="*60)
        
        # Create test file
        try:
            test_file = create_test_audio(fmt, duration_ms=3000)
            
            # Test conversion to PCM
            to_pcm_ok = test_conversion_to_pcm(test_file, fmt)
            
            # Test conversion from PCM
            from_pcm_ok = test_conversion_from_pcm(fmt)
            
            # Cleanup
            os.unlink(test_file)
            
            results[f'{fmt}_to_pcm'] = to_pcm_ok
            results[f'{fmt}_from_pcm'] = from_pcm_ok
            
        except Exception as e:
            print(f"\n✗ Format {fmt} test failed: {e}")
            import traceback
            traceback.print_exc()
            results[f'{fmt}_to_pcm'] = False
            results[f'{fmt}_from_pcm'] = False
    
    # Print summary
    print(f"\n{'='*60}")
    print("TEST SUMMARY")
    print("="*60)
    
    total = len(results)
    passed = sum(1 for v in results.values() if v)
    
    for test, result in sorted(results.items()):
        status = "✓ PASS" if result else "✗ FAIL"
        print(f"{status} - {test}")
    
    print(f"\n{passed}/{total} tests passed")
    
    if passed == total:
        print("\n🎉 All tests passed!")
        return 0
    else:
        print(f"\n⚠️  {total - passed} tests failed")
        return 1


if __name__ == '__main__':
    sys.exit(main())
