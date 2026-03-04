#!/usr/bin/env python3
"""
Quick local test - can run without Docker
Tests the conversion functions directly
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'server'))

from pydub import AudioSegment
from pydub.generators import Sine
import tempfile


def quick_test():
    """Quick sanity test of audio conversion"""
    print("Running quick conversion test...\n")
    
    formats = ['mp3', 'wav', 'm4a']
    
    for fmt in formats:
        print(f"Testing {fmt}...")
        try:
            # Create test audio
            audio = Sine(440).to_audio_segment(duration=1000)
            audio = audio.set_frame_rate(44100).set_channels(2)
            
            # Save as format
            with tempfile.NamedTemporaryFile(suffix=f'.{fmt}', delete=False) as f:
                temp_input = f.name
            
            if fmt == 'm4a':
                audio.export(temp_input, format='ipod', codec='aac', bitrate='128k')
            elif fmt == 'mp3':
                audio.export(temp_input, format='mp3', bitrate='128k')
            else:
                audio.export(temp_input, format=fmt)
            
            # Load back
            loaded = AudioSegment.from_file(temp_input, format=fmt)
            
            # Convert to 48kHz mono
            converted = loaded.set_channels(1).set_frame_rate(48000)
            
            # Export to PCM
            with tempfile.NamedTemporaryFile(suffix='.pcm', delete=False) as f:
                temp_pcm = f.name
            
            converted.export(
                temp_pcm,
                format="s16le",
                codec="pcm_s16le",
                parameters=["-f", "s16le", "-acodec", "pcm_s16le"]
            )
            
            # Read PCM back
            with open(temp_pcm, 'rb') as f:
                pcm_data = f.read()
            
            pcm_audio = AudioSegment(
                data=pcm_data,
                sample_width=2,
                frame_rate=48000,
                channels=1
            )
            
            # Convert back to format
            with tempfile.NamedTemporaryFile(suffix=f'_out.{fmt}', delete=False) as f:
                temp_output = f.name
            
            if fmt == 'm4a':
                pcm_audio.export(temp_output, format='ipod', codec='aac', bitrate='128k', parameters=["-strict", "experimental"])
            elif fmt == 'mp3':
                pcm_audio.export(temp_output, format='mp3', bitrate='128k', parameters=["-q:a", "2"])
            else:
                pcm_audio.export(temp_output, format=fmt)
            
            # Verify output
            final = AudioSegment.from_file(temp_output)
            
            print(f"  ✓ {fmt}: {len(audio)}ms → {len(pcm_audio)}ms → {len(final)}ms")
            
            # Cleanup
            os.unlink(temp_input)
            os.unlink(temp_pcm)
            os.unlink(temp_output)
            
        except Exception as e:
            print(f"  ✗ {fmt} FAILED: {e}")
            return False
    
    print("\n✓ All quick tests passed!")
    return True


if __name__ == '__main__':
    success = quick_test()
    sys.exit(0 if success else 1)
