#!/usr/bin/env python3
"""
End-to-end API tests for RNNoise server
"""
import os
import sys
import time
import tempfile
import requests
from pathlib import Path
from pydub import AudioSegment
from pydub.generators import Sine


def create_test_audio_file(format_name, duration_seconds=5):
    """Create a test audio file"""
    print(f"Creating test {format_name} file ({duration_seconds}s)...")
    
    # Generate sine wave
    audio = Sine(440).to_audio_segment(duration=duration_seconds * 1000)
    audio = audio.set_frame_rate(44100).set_channels(2)
    
    with tempfile.NamedTemporaryFile(suffix=f'.{format_name}', delete=False) as f:
        temp_path = f.name
    
    if format_name == 'm4a':
        audio.export(temp_path, format='ipod', codec='aac', bitrate='128k')
    elif format_name == 'mp3':
        audio.export(temp_path, format='mp3', bitrate='128k')
    elif format_name == 'wav':
        audio.export(temp_path, format='wav')
    elif format_name == 'ogg':
        audio.export(temp_path, format='ogg', codec='libvorbis')
    elif format_name == 'flac':
        audio.export(temp_path, format='flac')
    
    size = os.path.getsize(temp_path)
    print(f"✓ Created: {temp_path} ({size} bytes)")
    return temp_path


def test_health_endpoint(base_url):
    """Test /health endpoint"""
    print(f"\n{'='*60}")
    print("Testing /health endpoint")
    print("="*60)
    
    try:
        response = requests.get(f"{base_url}/health", timeout=5)
        
        if response.status_code == 200:
            data = response.json()
            print(f"✓ Status: {data.get('status')}")
            print(f"✓ Active requests: {data.get('active_requests')}")
            print(f"✓ Max concurrency: {data.get('max_concurrency')}")
            return True
        else:
            print(f"✗ Failed with status {response.status_code}")
            return False
            
    except Exception as e:
        print(f"✗ Error: {e}")
        return False


def test_denoise_direct(base_url, format_name, duration=3):
    """Test /denoise endpoint with direct upload"""
    print(f"\n{'='*60}")
    print(f"Testing /denoise endpoint - {format_name.upper()}")
    print("="*60)
    
    test_file = None
    output_file = None
    
    try:
        # Create test file
        test_file = create_test_audio_file(format_name, duration)
        
        # Read file
        with open(test_file, 'rb') as f:
            audio_data = f.read()
        
        print(f"Uploading {len(audio_data)} bytes...")
        
        # Send request
        start_time = time.time()
        response = requests.post(
            f"{base_url}/denoise?format={format_name}",
            data=audio_data,
            headers={'Content-Type': 'application/octet-stream'},
            timeout=60
        )
        elapsed = time.time() - start_time
        
        if response.status_code != 200:
            print(f"✗ Failed with status {response.status_code}")
            print(f"Response: {response.text[:500]}")
            return False
        
        # Save output
        output_file = tempfile.NamedTemporaryFile(suffix=f'_denoised.{format_name}', delete=False).name
        with open(output_file, 'wb') as f:
            f.write(response.content)
        
        output_size = len(response.content)
        print(f"✓ Received {output_size} bytes in {elapsed:.2f}s")
        
        # Verify output can be loaded
        try:
            output_audio = AudioSegment.from_file(output_file)
            print(f"✓ Output verified: {output_audio.frame_rate}Hz, {output_audio.channels}ch, {len(output_audio)}ms")
            return True
        except Exception as e:
            print(f"✗ Output file is invalid: {e}")
            return False
            
    except requests.Timeout:
        print(f"✗ Request timeout")
        return False
    except Exception as e:
        print(f"✗ Error: {e}")
        import traceback
        traceback.print_exc()
        return False
    finally:
        # Cleanup
        if test_file and os.path.exists(test_file):
            os.unlink(test_file)
        if output_file and os.path.exists(output_file):
            os.unlink(output_file)


def test_concurrent_requests(base_url, num_requests=3):
    """Test concurrent requests"""
    print(f"\n{'='*60}")
    print(f"Testing concurrent requests ({num_requests} simultaneous)")
    print("="*60)
    
    import threading
    
    results = []
    
    def make_request(thread_id):
        try:
            # Create small test file
            test_file = create_test_audio_file('mp3', duration=2)
            
            with open(test_file, 'rb') as f:
                audio_data = f.read()
            
            start = time.time()
            response = requests.post(
                f"{base_url}/denoise?format=mp3",
                data=audio_data,
                headers={'Content-Type': 'application/octet-stream'},
                timeout=60
            )
            elapsed = time.time() - start
            
            os.unlink(test_file)
            
            if response.status_code == 200:
                print(f"  Thread {thread_id}: ✓ Success in {elapsed:.2f}s")
                results.append(True)
            else:
                print(f"  Thread {thread_id}: ✗ Failed ({response.status_code})")
                results.append(False)
                
        except Exception as e:
            print(f"  Thread {thread_id}: ✗ Error - {e}")
            results.append(False)
    
    # Start threads
    threads = []
    start_time = time.time()
    
    for i in range(num_requests):
        t = threading.Thread(target=make_request, args=(i,))
        t.start()
        threads.append(t)
    
    # Wait for all
    for t in threads:
        t.join()
    
    total_time = time.time() - start_time
    
    passed = sum(results)
    print(f"\n✓ {passed}/{num_requests} requests succeeded")
    print(f"✓ Total time: {total_time:.2f}s")
    
    return passed == num_requests


def main():
    """Run all API tests"""
    base_url = os.getenv('RNNOISE_API_URL', 'http://localhost:8005')
    
    print("\n" + "="*60)
    print(f"RNNOISE API TESTS")
    print(f"Target: {base_url}")
    print("="*60)
    
    results = {}
    
    # Test 1: Health check
    results['health'] = test_health_endpoint(base_url)
    
    if not results['health']:
        print("\n⚠️  Server not responding. Is it running?")
        print(f"   docker logs rnnoise-server")
        return 1
    
    # Test 2: Different formats
    formats = ['mp3', 'm4a', 'wav']  # Test most common formats
    
    for fmt in formats:
        results[f'denoise_{fmt}'] = test_denoise_direct(base_url, fmt, duration=2)
    
    # Test 3: Concurrent requests
    results['concurrent'] = test_concurrent_requests(base_url, num_requests=3)
    
    # Summary
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
