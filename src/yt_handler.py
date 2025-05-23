from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from config import DOWNLOAD_DIR, TASK_CLEANUP_TIME, MAX_WORKERS
from src.json_utils import load_tasks, save_tasks, load_keys
from src.auth import check_memory_limit
import yt_dlp, os, threading, json, time, shutil, subprocess
from yt_dlp.utils import download_range_func

executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)

if not os.path.exists(DOWNLOAD_DIR):
    os.makedirs(DOWNLOAD_DIR)

def get_format_size(info, format_id):
    for f in info.get('formats', []):
        if f.get('format_id') == format_id:
            return f.get('filesize') or f.get('filesize_approx', 0)
    return 0

def get_best_format_size(info, formats, formats_list, is_video=True):
    if not formats_list:
        return 0
    formats_with_size = [f for f in formats_list if (f.get('filesize') or f.get('filesize_approx', 0)) > 0]
    
    if formats_with_size:
        if is_video:
            return max(formats_with_size, 
                        key=lambda f: (f.get('height', 0), f.get('tbr', 0)))
        else:
            return max(formats_with_size, 
                        key=lambda f: (f.get('abr', 0) or f.get('tbr', 0)))
    
    best_format = max(formats_list, 
                    key=lambda f: (f.get('height', 0), f.get('tbr', 0)) if is_video 
                    else (f.get('abr', 0) or f.get('tbr', 0)))
    
    if best_format.get('tbr'):
        estimated_size = int(best_format['tbr'] * info.get('duration', 0) * 128 * 1024 / 8)
        if estimated_size > 0:
            return best_format
    
    similar_formats = [f for f in formats if f.get('height', 0) == best_format.get('height', 0)] if is_video \
                    else [f for f in formats if abs(f.get('abr', 0) - best_format.get('abr', 0)) < 50]
    
    sizes = [f.get('filesize') or f.get('filesize_approx', 0) for f in similar_formats]
    if sizes and any(sizes):
        best_format['filesize_approx'] = max(s for s in sizes if s > 0)
        return best_format
    
    return best_format

def check_and_get_size(url, video_format=None, audio_format=None):
    try:
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'extract_flat': False,
            'skip_download': True,
            'cookiefile': '/app/youtube_cookies.txt'
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            formats = info['formats']
            total_size = 0
            
            if video_format:
                if video_format == 'bestvideo':
                    video_formats = [f for f in formats if f.get('vcodec') != 'none' and f.get('acodec') == 'none']
                    best_video = get_best_format_size(info, formats, video_formats, is_video=True)
                    total_size += best_video.get('filesize') or best_video.get('filesize_approx', 0)
                else:
                    format_info = next((f for f in formats if f.get('format_id') == video_format), None)
                    if format_info:
                        total_size += format_info.get('filesize') or format_info.get('filesize_approx', 0)

            if audio_format:
                if audio_format == 'bestaudio':
                    audio_formats = [f for f in formats if f.get('acodec') != 'none' and f.get('vcodec') == 'none']
                    best_audio = get_best_format_size(info, formats, audio_formats, is_video=False)
                    total_size += best_audio.get('filesize') or best_audio.get('filesize_approx', 0)
                else:
                    format_info = next((f for f in formats if f.get('format_id') == audio_format), None)
                    if format_info:
                        total_size += format_info.get('filesize') or format_info.get('filesize_approx', 0)
            total_size = int(total_size * 1.10)            
            return total_size if total_size > 0 else -1 
    except Exception as e:
        print(f"Error in check_and_get_size: {str(e)}")
        return -1

def get_info(task_id, url):
    try:
        tasks = load_tasks()
        tasks[task_id].update(status='processing')
        save_tasks(tasks)

        download_path = os.path.join(DOWNLOAD_DIR, task_id)
        # Use exist_ok=True to avoid error if directory already exists
        os.makedirs(download_path, exist_ok=True)

        ydl_opts = {'quiet': True, 'no_warnings': True, 'extract_flat': True, 'skip_download': True}

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)

            info_file = os.path.join(DOWNLOAD_DIR, task_id, f'info.json')
            os.makedirs(os.path.dirname(info_file), exist_ok=True)
            with open(info_file, 'w') as f:
                json.dump(info, f)

            tasks = load_tasks()
            tasks[task_id].update(status='completed')
            tasks[task_id]['completed_time'] = datetime.now().isoformat()
            tasks[task_id]['file'] = f'/files/{task_id}/info.json'
            save_tasks(tasks)
        except Exception as e:
            handle_task_error(task_id, e)
    except Exception as e:
        handle_task_error(task_id, e)

def get(task_id, url, type, video_format="bestvideo", audio_format="bestaudio"):
    try:
        tasks = load_tasks()
        tasks[task_id].update(status='processing')
        save_tasks(tasks)

        original_filename = None
        output_filename = None

        if type.lower() == 'audio':
            format_option = f'{audio_format}/bestaudio/best'
            output_template = f'audio.%(ext)s'
        else:
            format_option = f'{video_format}+{audio_format}/bestvideo+bestaudio/best'
            output_template = f'video.%(ext)s'

        key_name = tasks[task_id].get('key_name')
        keys = load_keys()
        if key_name not in keys:
            handle_task_error(task_id, "Invalid API key")
            return

        download_path = os.path.join(DOWNLOAD_DIR, task_id)
        # Use exist_ok=True to avoid error if directory already exists
        os.makedirs(download_path, exist_ok=True)

        ydl_opts = {
            'format': format_option,
            'outtmpl': {
                'default': os.path.join(download_path, output_template)
            },
            'merge_output_format': 'mp4' if type.lower() == 'video' else None,
            'cookiefile': '/app/youtube_cookies.txt',
            'progress_hooks': [lambda d: print(f'Download status: {d["_percent_str"]}') if d.get('status') == 'downloading' else None],
            'noplaylist': True, # Prevent downloading entire playlist
        }

        if tasks[task_id].get('start_time') or tasks[task_id].get('end_time'):
            start_time = tasks[task_id].get('start_time') or '00:00:00'
            end_time = tasks[task_id].get('end_time') or '10:00:00' # Consider setting a max duration or making it dynamic

            def time_to_seconds(time_str):
                try:
                    h, m, s = map(float, time_str.split(':'))
                    return h * 3600 + m * 60 + s
                except ValueError:
                    # Handle potential malformed time strings
                    print(f"Warning: Invalid time format '{time_str}', using default range.")
                    return None, None # Indicate error
                
            start_seconds = time_to_seconds(start_time)
            end_seconds = time_to_seconds(end_time)

            if start_seconds is not None and end_seconds is not None:
                ydl_opts['download_ranges'] = download_range_func(None, [(start_seconds, end_seconds)])
                ydl_opts['force_keyframes_at_cuts'] = tasks[task_id].get('force_keyframes', True) # Default to True for cuts
            else:
                # Handle case where time conversion failed - maybe skip cutting?
                print("Skipping time-based cutting due to invalid format.")

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info_dict = ydl.extract_info(url, download=True)
                # yt-dlp >= 2023.06.22 returns the final filename in info_dict['requested_downloads'][0]['filepath']
                original_filename = info_dict.get('requested_downloads', [{}])[0].get('filepath')
                if not original_filename:
                    # Fallback for older yt-dlp or unexpected structures
                    downloaded_files = [f for f in os.listdir(download_path) if not f.endswith('.part') and not f.endswith('_compliant.mp4')]
                    if downloaded_files:
                        original_filename = os.path.join(download_path, downloaded_files[0])
                    else:
                         raise Exception("Could not determine downloaded file name.")

            # --- Start FFmpeg Re-encoding --- 
            if type.lower() == 'video':
                print(f"Starting FFmpeg re-encoding for {original_filename}")
                base, ext = os.path.splitext(os.path.basename(original_filename))
                compliant_filename = f"{base}_compliant.mp4"
                output_filepath = os.path.join(download_path, compliant_filename)

                ffmpeg_command = [
                    'ffmpeg',
                    '-i', original_filename,
                    '-c:v', 'libx264',
                    '-b:v', '5000k',
                    '-c:a', 'aac',
                    '-b:a', '128k',
                    '-strict', 'experimental', # For AAC compatibility
                    '-y', # Overwrite output file if it exists
                    output_filepath
                ]
                
                try:
                    print(f"Running FFmpeg: {' '.join(ffmpeg_command)}")
                    process = subprocess.run(ffmpeg_command, check=True, capture_output=True, text=True)
                    print("FFmpeg re-encoding completed successfully.")
                    output_filename = output_filepath # Set the final output filename
                    # Clean up original file
                    try:
                        os.remove(original_filename)
                        print(f"Removed original file: {original_filename}")
                    except OSError as e:
                        print(f"Error removing original file {original_filename}: {e}")
                except subprocess.CalledProcessError as e:
                    print(f"FFmpeg error output:\n{e.stderr}")
                    raise Exception(f"FFmpeg re-encoding failed: {e.stderr}") 
                except Exception as e: # Catch other potential errors during ffmpeg step
                    print(f"Unexpected error during FFmpeg processing: {e}")
                    raise # Re-raise to be caught by outer handler
            else:
                # For audio or if re-encoding is skipped, use the original file
                output_filename = original_filename
            # --- End FFmpeg Re-encoding ---
            
            tasks = load_tasks() # Reload tasks in case state changed
            tasks[task_id].update(status='completed')
            tasks[task_id]['completed_time'] = datetime.now().isoformat()
            # Use the final output filename (either original or compliant)
            tasks[task_id]['file'] = f'/files/{task_id}/{os.path.basename(output_filename)}'
            save_tasks(tasks)
            print(f"Task {task_id} completed. File: {tasks[task_id]['file']}")

        except Exception as e:
            handle_task_error(task_id, e)
    except Exception as e:
        # Catch errors in initial task loading/saving
        handle_task_error(task_id, e)

def get_live(task_id, url, type, start, duration, video_format="bestvideo", audio_format="bestaudio"):
    try:
        tasks = load_tasks()
        tasks[task_id].update(status='processing')
        save_tasks(tasks)
        
        download_path = os.path.join(DOWNLOAD_DIR, task_id)
        # Use exist_ok=True to avoid error if directory already exists
        os.makedirs(download_path, exist_ok=True)

        current_time = int(time.time())
        start_time = current_time - start
        end_time = start_time + duration

        if type.lower() == 'audio':
            format_option = f'{audio_format}'
            output_template = f'live_audio.%(ext)s'
        else:
            format_option = f'{video_format}+{audio_format}'
            output_template = f'live_video.%(ext)s'

        ydl_opts = {
            'format': format_option,
            'outtmpl': os.path.join(download_path, output_template),
            'download_ranges': lambda info, *args: [{'start_time': start_time, 'end_time': end_time,}],
            'merge_output_format': 'mp4' if type.lower() == 'video' else None,
            'cookiefile': '/app/youtube_cookies.txt'
        }
        
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
            tasks = load_tasks()
            tasks[task_id].update(status='completed')
            tasks[task_id]['completed_time'] = datetime.now().isoformat()
            tasks[task_id]['file'] = f'/files/{task_id}/' + os.listdir(download_path)[0]
            save_tasks(tasks)
        except Exception as e:
            handle_task_error(task_id, e)
    except Exception as e:
        handle_task_error(task_id, e)

def handle_task_error(task_id, error):
    tasks = load_tasks()
    tasks[task_id].update(status='error', error=str(error), completed_time=datetime.now().isoformat())
    save_tasks(tasks)
    print(f"Error in task {task_id}: {str(error)}")

def cleanup_task(task_id):
    tasks = load_tasks()
    download_path = os.path.join(DOWNLOAD_DIR, task_id)
    if os.path.exists(download_path):
        shutil.rmtree(download_path, ignore_errors=True)
    if task_id in tasks:
        del tasks[task_id]
        save_tasks(tasks)

def cleanup_orphaned_folders():
    tasks = load_tasks()
    task_ids = set(tasks.keys())
    
    for folder in os.listdir(DOWNLOAD_DIR):
        folder_path = os.path.join(DOWNLOAD_DIR, folder)
        if os.path.isdir(folder_path) and folder not in task_ids:
            shutil.rmtree(folder_path, ignore_errors=True)
            print(f"Removed orphaned folder: {folder_path}")

def cleanup_processing_tasks():
    tasks = load_tasks()
    for task_id, task in list(tasks.items()):
        if task['status'] == 'processing':
            task['status'] = 'error'
            task['error'] = 'Task was interrupted during processing'
            task['completed_time'] = datetime.now().isoformat()
    save_tasks(tasks)

def process_tasks():
    while True:
        tasks = load_tasks()
        current_time = datetime.now()
        for task_id, task in list(tasks.items()):
            if task['status'] == 'waiting':
                # Use .get() with defaults to handle older tasks missing format keys
                video_format = task.get('video_format', 'bestvideo')
                audio_format = task.get('audio_format', 'bestaudio')

                if task['task_type'] == 'get_video':
                    executor.submit(get, task_id, task['url'], 'video', video_format, audio_format)
                elif task['task_type'] == 'get_audio':
                    # Audio tasks primarily need audio_format, video_format can default
                    executor.submit(get, task_id, task['url'], 'audio', 'bestvideo', audio_format)
                elif task['task_type'] == 'get_info':
                    executor.submit(get_info, task_id, task['url'])
                elif task['task_type'] == 'get_live_video':
                    executor.submit(get_live, task_id, task['url'], 'video', task['start'], task['duration'], video_format, audio_format)
                elif task['task_type'] == 'get_live_audio':
                    # Live audio tasks primarily need audio_format
                    executor.submit(get_live, task_id, task['url'], 'audio', task['start'], task['duration'], 'bestvideo', audio_format)
            elif task['status'] in ['completed', 'error']:
                # Ensure completed_time exists before parsing
                completed_time_str = task.get('completed_time')
                if completed_time_str:
                    try:
                        completed_time = datetime.fromisoformat(completed_time_str)
                        if current_time - completed_time > timedelta(minutes=TASK_CLEANUP_TIME):
                            cleanup_task(task_id)
                    except ValueError:
                        print(f"Warning: Invalid date format for completed_time in task {task_id}: {completed_time_str}")
                        # Optionally handle the error, e.g., remove the task or set a default cleanup time
                        # cleanup_task(task_id) # Uncomment to cleanup tasks with invalid dates
        if current_time.minute % 5 == 0 and current_time.second == 0:
            cleanup_orphaned_folders()
        time.sleep(1)

cleanup_processing_tasks()
cleanup_orphaned_folders()
thread = threading.Thread(target=process_tasks, daemon=True)
thread.start()
