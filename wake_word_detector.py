#!/usr/bin/env python3
"""
Wake Word Detection Handler

This module handles wake word detection using the openWakeWord library.
It processes audio chunks and detects when the wake word is spoken.
"""

import os
import numpy as np
import random
from typing import Dict, Any, Optional, Tuple # Not strictly needed for this file to run
# Python 3.9+ has built-in types for these, or they are not used complexly here

# Load environment variables from .env file if not already loaded
from dotenv import load_dotenv
load_dotenv() # Ensures .env is loaded when this module is imported or run

# Print Python path to help with debugging - only when run directly
if __name__ == "__main__":
    import sys
    print("--- wake_word_detector.py (direct run) ---")
    print("Python path:", sys.path)
    print("Python executable:", sys.executable)

# Function to download models using the correct method
def download_openwakeword_models_internal(): # Renamed to avoid conflict if this file name was 'download_models'
    """
    Download openwakeword models using the utils.download_models function.
    """
    try:
        from openwakeword.utils import download_models
        print("wake_word_detector.py: Downloading openWakeWord models if missing...")
        download_models() # This will download all default models if not present
        print("wake_word_detector.py: Models are ready!")
        return True
    except Exception as e:
        print(f"wake_word_detector.py: Failed to download models: {e}")
        return False

# Try to find openwakeword in installed packages - only when run directly
if __name__ == "__main__":
    try:
        import pkg_resources
        installed_packages = [pkg.key for pkg in pkg_resources.working_set]
        if 'openwakeword' in installed_packages:
            print("wake_word_detector.py: openwakeword is installed in the current environment")
        else:
            print("wake_word_detector.py: openwakeword is NOT found in installed packages")
    except Exception as e:
        print(f"wake_word_detector.py: Error checking installed packages: {e}")

# Import openWakeWord - we'll handle import errors gracefully
OPENWAKEWORD_AVAILABLE = False
OpenWakeWordModel = None
download_models_func = None

try:
    print("wake_word_detector.py: Attempting to import openwakeword...")
    import openwakeword
    if __name__ == "__main__":
        print(f"wake_word_detector.py: openwakeword package found at: {openwakeword.__file__}")
    
    from openwakeword.model import Model as OWWModel # Alias to avoid confusion
    OpenWakeWordModel = OWWModel # Assign to the expected name

    try:
        from openwakeword.utils import download_models
        if __name__ == "__main__":
            print("wake_word_detector.py: Successfully imported openwakeword.utils.download_models")
        download_models_func = download_models
    except ImportError: # Changed from Exception to ImportError for specificity
        if __name__ == "__main__":
            print(f"wake_word_detector.py: Warning: Could not import openwakeword.utils.download_models.")
            print("wake_word_detector.py: Using custom download function instead.")
        download_models_func = download_openwakeword_models_internal
    
    OPENWAKEWORD_AVAILABLE = True
    print("wake_word_detector.py: Successfully imported and configured openwakeword components.")
except ImportError as e:
    print(f"wake_word_detector.py: ImportError: {e}")
    print("wake_word_detector.py: Warning: openWakeWord not installed or found. Wake word detection will not function.")
    print("wake_word_detector.py: To install openWakeWord, try: pip install openwakeword")
    
    # Create dummy classes for graceful degradation if openwakeword is not available
    class DummyOpenWakeWordModel: # Renamed to avoid conflict
        def __init__(self, *args, **kwargs):
            print("wake_word_detector.py: Using dummy OpenWakeWordModel - wake word detection disabled")
        
        def predict(self, *args, **kwargs):
            return {} # Return empty dict (no wake word detected)
        def reset(self): # Add reset method to dummy
            pass
    
    OpenWakeWordModel = DummyOpenWakeWordModel # Assign dummy to the expected name

    def dummy_download_models_func(*args, **kwargs):
        print("wake_word_detector.py: Cannot download models - openwakeword not installed properly")
        return False
    download_models_func = dummy_download_models_func


class WakeWordDetector:
    """
    Handles wake word detection using openWakeWord.
    """
    
    def __init__(self,
                 wake_word_model: Optional[str] = None,
                 threshold: Optional[float] = None,
                 sample_rate: int = 16000):
        print(f"WakeWordDetector: Initializing... OPENWAKEWORD_AVAILABLE is {OPENWAKEWORD_AVAILABLE}")
        self.wake_word_model_name = wake_word_model or os.environ.get("WAKE_WORD_MODEL", "hey_jarvis") # Use a default like hey_jarvis
        
        threshold_str = os.environ.get("WAKE_WORD_THRESHOLD", "0.5")
        self.threshold = threshold if threshold is not None else float(threshold_str)
        
        self.model_type = os.environ.get("WAKE_WORD_MODEL_TYPE", "onnx").lower()
        self.sample_rate = sample_rate # This is the rate of audio coming IN to process_audio
        self.oww_expected_rate = 16000 # openWakeWord models expect 16kHz

        self.model = None # Initialize model attribute
        
        if OPENWAKEWORD_AVAILABLE and OpenWakeWordModel is not None and download_models_func is not None:
            try:
                print(f"WakeWordDetector: Ensuring wake word models are downloaded using: {download_models_func.__name__}")
                # Ensure the specific model is available or attempt download
                # download_models_func(model_names=[self.wake_word_model_name]) # More targeted download
                download_models_func() # General download for simplicity now

                model_file_name_to_load = f"{self.wake_word_model_name}.{self.model_type}"
                if self.model_type == "tflite" and not model_file_name_to_load.endswith(".tflite"): # openwakeword v0.5+ uses .tflite
                    model_file_name_to_load = f"{self.wake_word_model_name}.tflite"
                elif self.model_type == "onnx" and not model_file_name_to_load.endswith(".onnx"):
                     model_file_name_to_load = f"{self.wake_word_model_name}.onnx"


                print(f"WakeWordDetector: Attempting to load model: '{model_file_name_to_load}' with inference_framework='{self.model_type}'")
                
                # For openwakeword >0.5, wakeword_models should be list of base names, not filenames
                self.model = OpenWakeWordModel(
                    wakeword_models=[self.wake_word_model_name], # Pass base name like "hey_jarvis"
                    inference_framework=self.model_type
                )
                
                # Verify model loaded
                if not self.model.models: # .models is the dict of loaded models
                    print(f"WakeWordDetector: ERROR - Model list is empty after initialization for '{self.wake_word_model_name}'.")
                    self.model = None # Failed to load
                else:
                    print(f"WakeWordDetector: Successfully initialized with model: {self.wake_word_model_name} (using {self.model_type}). Loaded models: {list(self.model.models.keys())}")

            except Exception as e:
                print(f"WakeWordDetector: Error initializing openWakeWord model '{self.wake_word_model_name}': {e}")
                self.model = None # Ensure model is None on failure
        else:
            print("WakeWordDetector: openWakeWord not available or core components not imported. Using dummy model.")
            self.model = OpenWakeWordModel() # This will be DummyOpenWakeWordModel if import failed

        self.buffer = np.array([], dtype=np.int16) # Store as int16, convert to float32 for predict
        self._config_printed = False
        self._raw_values_info_printed = False
        self._resampling_info_printed = False
        self._scipy_checked = False
        self._scipy_available = False

    def _check_scipy(self):
        if not self._scipy_checked:
            try:
                import scipy.signal
                self._scipy_available = True
                print("WakeWordDetector: Scipy is available for resampling.")
            except ImportError:
                self._scipy_available = False
                print("WakeWordDetector: Scipy not installed, resampling will not be performed. Ensure input audio is 16kHz.")
            self._scipy_checked = True
        return self._scipy_available

    def process_audio(self, audio_chunk_bytes: bytes) -> bool:
        if not self._config_printed:
            print(f"WakeWordDetector.process_audio: Config: model={self.wake_word_model_name}, threshold={self.threshold}, input_rate={self.sample_rate}Hz")
            self._config_printed = True
            
        if self.model is None or not hasattr(self.model, 'predict'): # Check if it's a valid model object
            # This also handles the case where self.model became DummyOpenWakeWordModel and predict is a dummy
            if not isinstance(self.model, DummyOpenWakeWordModel): # Avoid double printing for dummy
                 print("WakeWordDetector.process_audio: No valid model loaded, cannot process audio.")
            return False
        
        audio_data_int16 = np.frombuffer(audio_chunk_bytes, dtype=np.int16)

        # Resample if input rate is not 16kHz and scipy is available
        if self.sample_rate != self.oww_expected_rate:
            if self._check_scipy():
                try:
                    num_samples_input = len(audio_data_int16)
                    num_samples_output = int(num_samples_input * self.oww_expected_rate / self.sample_rate)
                    if num_samples_output > 0: # Avoid resampling to zero samples
                        from scipy import signal # Import here as it's checked
                        resampled_audio_float32 = signal.resample(audio_data_int16.astype(np.float32), num_samples_output)
                        audio_data_int16 = resampled_audio_float32.astype(np.int16) # openwakeword expects int16
                        if not self._resampling_info_printed:
                            print(f"WakeWordDetector: Resampled audio from {self.sample_rate}Hz to {self.oww_expected_rate}Hz. Chunk {num_samples_input} -> {num_samples_output} samples.")
                            self._resampling_info_printed = True
                    else: # Resampling would result in 0 samples, skip
                         if not self._resampling_info_printed: # Print once
                            print(f"WakeWordDetector: WARN - Resampling from {self.sample_rate}Hz to {self.oww_expected_rate}Hz would result in 0 samples for this chunk size. Skipping resampling. Ensure audio chunks are large enough or rates match.")
                            self._resampling_info_printed = True # Suppress further warnings for this
                except Exception as e:
                    print(f"WakeWordDetector: Error during resampling: {e}")
            # If scipy not available, warning printed by _check_scipy, audio passes through at original rate
        
        # openWakeWord expects int16 numpy array
        # For versions like 0.5.x, it seems to handle internal buffering well,
        # so we can feed it chunks directly.
        prediction = self.model.predict(audio_data_int16) # Pass the int16 numpy array

        # The key in the prediction dictionary should be the base model name, e.g., "hey_jarvis"
        # (not "hey_jarvis.onnx") for openwakeword versions >= 0.5.0
        score = prediction.get(self.wake_word_model_name, 0.0) 
        
        if score > self.threshold:
            print(f"WakeWordDetector: DETECTED '{self.wake_word_model_name}' with score {score:.4f} (threshold {self.threshold})")
            return True
        
        # Optional: print scores if they are close to threshold for debugging
        # elif score > self.threshold * 0.5: # e.g. if score is more than half the threshold
        #    print(f"WakeWordDetector: Near miss for '{self.wake_word_model_name}', score: {score:.4f}")

        return False
    
    def reset(self):
        if self.model and hasattr(self.model, 'reset'):
            self.model.reset()
        self.buffer = np.array([], dtype=np.int16) # Reset buffer
        print("WakeWordDetector: Reset complete.")

# Example usage when run directly
if __name__ == "__main__":
    print("\n--- Running wake_word_detector.py directly for testing ---")
    # Test if OPENWAKEWORD_AVAILABLE is True and if OpenWakeWordModel is not the dummy
    if OPENWAKEWORD_AVAILABLE and OpenWakeWordModel is not None and not isinstance(OpenWakeWordModel(), DummyOpenWakeWordModel):
        print("Test: openWakeWord seems to be available and imported correctly.")
        
        # Attempt to create a detector instance for testing
        # This will use WAKE_WORD_MODEL from .env, e.g., "hey_jarvis"
        print("\nTest: Creating WakeWordDetector instance...")
        try:
            test_detector = WakeWordDetector(sample_rate=16000) # Assume 16kHz for direct test
            if test_detector.model and not isinstance(test_detector.model, DummyOpenWakeWordModel):
                print("Test: WakeWordDetector instance created successfully with a real model.")
                print(f"Test: Detector configured for model: {test_detector.wake_word_model_name}, threshold: {test_detector.threshold}")

                # Simulate some audio chunks
                print("\nTest: Simulating audio processing...")
                silence = np.zeros(1600, dtype=np.int16) # 0.1s of silence at 16kHz
                # To actually test detection, you'd need a real audio sample with the wake word.
                # For now, we just test that process_audio runs without error.
                for i in range(5):
                    detected = test_detector.process_audio(silence.tobytes())
                    if detected:
                        print(f"Test: Simulated detection occurred on iteration {i} (unexpected with silence).")
                    # time.sleep(0.1) # Not needed for byte simulation
                print("Test: Audio processing simulation complete.")
                test_detector.reset()

            else:
                print("Test: WakeWordDetector instance created, but using a dummy model or model loading failed.")
        except Exception as e:
            print(f"Test: Error creating or testing WakeWordDetector instance: {e}")
    else:
        print("Test: openWakeWord is NOT available or not imported correctly. Cannot perform detailed tests.")

    print("\n--- End of wake_word_detector.py direct run test ---")