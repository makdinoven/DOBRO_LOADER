import re
import webview
import uuid
import os
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from downloader_logic import KinescopeLogic
import s3_manager


class Api:
    def __init__(self):
        self.logic = None
        self.tasks = {}  # Храним инфо о задачах: {id: {info, progress_states}}
        # Процессор очереди скачиваний (максимум 3 одновременно)
        self.executor = ThreadPoolExecutor(max_workers=3)
        self.s3_config = s3_manager.load_config()

    def _get_window(self):
        return webview.windows[0] if webview.windows else None

    def send_log(self, task_id, message):
        window = self._get_window()
        if not window: return

        # Лог в интерфейс
        safe_msg = message.replace("'", "\\'").replace("\n", "").replace("\r", "")
        window.evaluate_js(f"addTaskLog('{task_id}', '{safe_msg}')")

        # Прогресс
        progress_match = re.search(r'(\d+\.?\d*)%', message)
        if progress_match:
            percent = float(progress_match.group(1))
            task = self.tasks.get(task_id)
            if task:
                if "Vid" in message:
                    task['progress']['video'] = percent
                elif "Aud" in message:
                    task['progress']['audio'] = percent

                avg = (task['progress'].get('video', 0) + task['progress'].get('audio', 0)) / 2
                window.evaluate_js(f"updateTaskProgress('{task_id}', {avg}, 'Загрузка...')")

        if "Merging" in message or "Muxing" in message:
            window.evaluate_js(f"updateTaskProgress('{task_id}', 100, 'Склейка...')")

    def select_folder(self):
        window = self._get_window()
        result = window.create_file_dialog(
            webview.FOLDER_DIALOG
        )
        if result and len(result) > 0:
            return result[0]
        return None

    def select_json(self):
        window = self._get_window()
        results = window.create_file_dialog(
            webview.OPEN_DIALOG,
            file_types=('JSON (*.json)',),
            allow_multiple=True
        )

        if not results: return None

        new_tasks = []
        for path in results:
            try:
                if not self.logic: self.logic = KinescopeLogic(lambda x: None)

                # Теперь extract_from_json возвращает СПИСОК
                video_list = self.logic.extract_from_json(path)

                for video_info in video_list:
                    task_id = str(uuid.uuid4())[:8]

                    # Получаем качества для конкретного видео
                    qualities = []
                    item = video_info['video_data']
                    if 'frameRate' in item:
                        qualities = sorted([int(q) for q in item['frameRate'].keys() if q.isdigit()], reverse=True)

                    self.tasks[task_id] = {
                        'info': video_info,
                        'progress': {'video': 0, 'audio': 0},
                        'path': path
                    }

                    new_tasks.append({
                        "id": task_id,
                        "filename": video_info['title'],
                        "qualities": qualities or [1080, 720, 480, 360]
                    })
            except Exception as e:
                print(f"Ошибка при чтении {path}: {e}")
                continue

        return new_tasks

    def save_s3_config(self, endpoint, access_key, secret_key, bucket, s3_path):
        """Сохраняет S3/R2 конфиг в зашифрованный файл."""
        config = {
            "endpoint": endpoint.strip(),
            "access_key": access_key.strip(),
            "secret_key": secret_key.strip(),
            "bucket": bucket.strip(),
            "s3_path": s3_path.strip().strip("/"),
        }
        s3_manager.save_config(config)
        self.s3_config = config
        return True

    def load_s3_config(self):
        """Возвращает сохранённый S3 конфиг (без secret_key для безопасности)."""
        config = s3_manager.load_config()
        if not config:
            return None
        return {
            "endpoint": config.get("endpoint", ""),
            "access_key": config.get("access_key", ""),
            "secret_key_set": bool(config.get("secret_key")),
            "bucket": config.get("bucket", ""),
            "s3_path": config.get("s3_path", ""),
        }

    def clear_s3_config(self):
        """Удаляет сохранённый S3 конфиг."""
        self.s3_config = None
        if os.path.exists(s3_manager.CONFIG_FILE):
            os.remove(s3_manager.CONFIG_FILE)
        return True

    def delete_task(self, task_id):
        if task_id in self.tasks:
            del self.tasks[task_id]
            # Можно добавить принудительную очистку логов, если нужно
            return True
        return False

    def start_download(self, task_id, quality, custom_folder=None, custom_name=None, upload_s3=False, s3_path_override=None):
        task = self.tasks.get(task_id)
        if not task: return

        def run():
            try:
                # Создаем экземпляр логики специально для этой задачи, чтобы лог шел правильно
                task_logic = KinescopeLogic(lambda msg: self.send_log(task_id, msg))

                base_dir = custom_folder if custom_folder else os.path.dirname(task['path'])

                # Имя файла: кастомное (если задано и отличается от оригинала) или из JSON
                original_title = task['info']['title'].strip()
                file_title = custom_name.strip() if custom_name and custom_name.strip() else original_title

                save_path = os.path.join(
                    base_dir,
                    re.sub(r'[\s\\/:*?"<>|]', '_', original_title) + f"_{quality}p.mp4"
                )

                final_path = os.path.join(
                    base_dir,
                    re.sub(r'[\s\\/:*?"<>|]', '_', file_title) + f"_{quality}p.mp4"
                )

                if os.path.exists(final_path):
                    self.send_log(task_id, f"✅ Файл уже существует: {final_path}. Пропуск.")
                    self._get_window().evaluate_js(f"updateTaskProgress('{task_id}', 100, 'Уже скачано')")
                    # Даже если файл уже есть — загрузим в S3 если нужно
                    if upload_s3:
                        self._upload_to_s3(task_id, final_path, s3_path_override)
                    return

                self.send_log(task_id, f"🚀 Очередь дошла, старт: {quality}p в {base_dir}")

                try:
                    success = task_logic.download_pipeline(task['info'], quality, save_path)
                except Exception as e:
                    self.send_log(task_id, f"❌ Системная ошибка скачивания: {str(e)}\n{traceback.format_exc()}")
                    success = False

                if success:
                    # Переименовываем файл если задано кастомное имя
                    if save_path != final_path and os.path.exists(save_path):
                        os.rename(save_path, final_path)
                        self.send_log(task_id, f"📝 Переименовано: {os.path.basename(final_path)}")

                    # Загружаем в S3 если включено
                    if upload_s3:
                        self._upload_to_s3(task_id, final_path, s3_path_override)

                    self.send_log(task_id, "✅ ГОТОВО")
                    self._get_window().evaluate_js(f"updateTaskProgress('{task_id}', 100, 'Завершено')")
                else:
                    self.send_log(task_id, "❌ ОШИБКА")
                    self._get_window().evaluate_js(f"updateTaskProgress('{task_id}', 0, 'Ошибка')")
                    
            except Exception as e:
                self.send_log(task_id, f"❌ КРИТИЧЕСКАЯ ОШИБКА ПОТОКА: {str(e)}\n{traceback.format_exc()}")
                self._get_window().evaluate_js(f"updateTaskProgress('{task_id}', 0, 'Критическая ошибка')")

        self.send_log(task_id, "⏳ Ожидание в очереди...")
        self._get_window().evaluate_js(f"updateTaskProgress('{task_id}', 0, 'В очереди...')")
        self.executor.submit(run)

    def _upload_to_s3(self, task_id, file_path, s3_path_override=None):
        """Загружает файл в S3/R2."""
        config = self.s3_config
        if not config:
            self.send_log(task_id, "⚠️ S3 не настроен, пропуск загрузки")
            return

        s3_prefix = s3_path_override.strip().strip("/") if s3_path_override else config.get("s3_path", "")
        filename = os.path.basename(file_path)
        s3_key = f"{s3_prefix}/{filename}" if s3_prefix else filename

        self._get_window().evaluate_js(f"updateTaskProgress('{task_id}', 100, 'S3 загрузка...')")
        s3_manager.upload_file(
            config, file_path, s3_key,
            log_fn=lambda msg: self.send_log(task_id, msg),
        )


def main():
    api = Api()
    webview.create_window(
        'DOBRO LOADER PRO', 'index.html', js_api=api,
        width=900, height=500, resizable=True
    )
    webview.start()


if __name__ == '__main__':
    main()