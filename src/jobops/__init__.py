import re
import sys
import os
import logging
import base64
from pathlib import Path
import threading
from urllib.parse import urlparse
import webbrowser
from PIL import Image
from io import BytesIO
from PySide6.QtCore import Signal, QFileSystemWatcher
import json
from jobops.utils import ResourceManager, NotificationService, check_platform_compatibility, create_desktop_entry
import uuid
import subprocess
from jobops.models import Document, DocumentType


# Qt imports
try:
    from PySide6.QtWidgets import *
    from PySide6.QtCore import *
    from PySide6.QtGui import *
    QT_AVAILABLE = True
except ImportError:
    try:
        from PyQt6.QtWidgets import *
        from PyQt6.QtCore import *
        from PyQt6.QtGui import *
        QT_AVAILABLE = True
    except ImportError:
        QT_AVAILABLE = False
        print("Neither PySide6 nor PyQt6 is installed. Please install one of them.")
        sys.exit(1)


from dotenv import load_dotenv
from jobops.models import DocumentType

# Embedded base64 icon data (64x64 PNG icon)
EMBEDDED_ICON_DATA = """
iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAYAAACqaXHeAAAABHNCSVQICAgIfAhkiAAAAAlwSFlzAAAOxAAADsQBlSsOGwAAABl0RVh0U29mdHdhcmUAd3d3Lmlua3NjYXBlLm9yZ5vuPBoAAAOxSURBVHic7ZtNaBNBFMefJFqrtVZbW6u01lq1Wq21aq3VWmut1lqrtdZqrdVaq7VWa63VWqu1VmuttVprtdZqrdVaq7VWa63VWqu1VmuttVprtdZqrdVaq7VWa63VWqu1VmuttVprtdZqrdVaq7VWa63VWqu1VmuttVprtdZqrdVaq7VWa60AAAD//2Q=="
"""

try:
    icon_data = base64.b64decode(EMBEDDED_ICON_DATA)
    img = Image.open(BytesIO(icon_data))
    img.show()
except Exception as e:
    print("Failed to load image:", e)

load_dotenv()

# Patch logging to always include span_id
class SpanIdLogFilter(logging.Filter):
    def filter(self, record):
        if not hasattr(record, 'span_id') or record.span_id is None:
            record.span_id = getattr(record, '_span_id', None) or str(uuid.uuid4())
        return True

logging.getLogger().addFilter(SpanIdLogFilter())

def get_span_id():
    return str(uuid.uuid4())

class JobInputDialog(QDialog):
    """Job input dialog: user provides URL (as a link) and markdown manually."""
    job_data_ready = Signal(dict)
    
    def __init__(self, app_instance=None):
        super().__init__()
        self.app_instance = app_instance
        self.setWindowTitle("Generate Motivation Letter")
        self.setFixedSize(700, 400)
        self.setWindowIcon(ResourceManager.create_app_icon())
        self._setup_ui()
        self._last_crawled_url = None
        self._last_crawled_markdown = None

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        # URL input (just stored as a link)
        url_layout = QHBoxLayout()
        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("Paste job URL here (optional, for reference)...")
        url_layout.addWidget(QLabel("Job URL:"))
        url_layout.addWidget(self.url_input)
        layout.addLayout(url_layout)
        self.url_input.editingFinished.connect(self._on_url_pasted)
        # Markdown job description (user-provided)
        layout.addWidget(QLabel("Job Description (Markdown, required):"))
        self.markdown_edit = QTextEdit()
        self.markdown_edit.setPlaceholderText("Paste or write the job description here in markdown format...")
        self.markdown_edit.setMinimumHeight(300)
        layout.addWidget(self.markdown_edit)
        # Buttons
        button_layout = QHBoxLayout()
        self.generate_btn = QPushButton("Generate Letter")
        self.cancel_btn = QPushButton("Cancel")
        self.generate_btn.clicked.connect(self.generate_letter)
        self.cancel_btn.clicked.connect(self.reject)
        button_layout.addWidget(self.generate_btn)
        button_layout.addWidget(self.cancel_btn)
        layout.addLayout(button_layout)

    def _on_url_pasted(self):
        url = self.url_input.text().strip()
        span_id = get_span_id()
        logging.info(f"URL pasted: {url}", extra={"span_id": span_id})
        if url and url == self._last_crawled_url and self._last_crawled_markdown:
            logging.info("URL already crawled, reusing existing markdown.", extra={"span_id": span_id})
            self.markdown_edit.setPlainText(self._last_crawled_markdown)
            return
        if not url or self.markdown_edit.toPlainText().strip():
            logging.info("No URL or markdown already present, skipping extraction.", extra={"span_id": span_id})
            return
        import re
        if not re.match(r"^https?://", url):
            logging.info("URL does not match http/https, skipping extraction.", extra={"span_id": span_id})
            return
        wait_dialog = QMessageBox(self)
        wait_dialog.setWindowTitle("Extracting Markdown")
        wait_dialog.setText("Extracting job description from URL. Please wait...")
        wait_dialog.setStandardButtons(QMessageBox.NoButton)
        wait_dialog.show()
        QApplication.processEvents()
        try:
            from jobops.scrapers import extract_markdown_from_url
            markdown = extract_markdown_from_url(url)
            self.markdown_edit.setPlainText(markdown)
            self._last_crawled_url = url
            self._last_crawled_markdown = markdown
        except Exception as e:
            logging.error(f"Markdown extraction error: {e}", extra={"span_id": span_id})
            QMessageBox.warning(self, "Extraction Error", f"Could not extract markdown from URL: {e}")
        finally:
            wait_dialog.done(0)

    def generate_letter(self):
        url = self.url_input.text().strip()
        markdown = self.markdown_edit.toPlainText().strip()
        if not markdown:
            QMessageBox.warning(self, "Error", "Job description in markdown is required.")
            return
        # Detect language of the job description
        try:
            from langdetect import detect
            detected_language = detect(markdown)
        except Exception:
            detected_language = "en"
        self.job_data = {
            'url': url,
            'job_markdown': markdown,
            'detected_language': detected_language
        }
        self.job_data_ready.emit(self.job_data)
        self.accept()

class UploadDialog(QDialog):
    """File upload dialog"""
    upload_data_ready = Signal(str, str)
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Upload Document")
        self.setFixedSize(400, 200)
        self.setWindowIcon(ResourceManager.create_app_icon())
        
        self.file_path = None
        self.doc_type = None
        self.setup_ui()
    
    def setup_ui(self):
        layout = QVBoxLayout(self)
        
        # File selection
        file_layout = QHBoxLayout()
        self.file_input = QLineEdit()
        self.file_input.setPlaceholderText("Select a file...")
        self.browse_btn = QPushButton("Browse")
        self.browse_btn.clicked.connect(self.browse_file)
        
        file_layout.addWidget(self.file_input)
        file_layout.addWidget(self.browse_btn)
        layout.addLayout(file_layout)
        
        # Document type dropdown
        type_group = QGroupBox("Document Type")
        type_layout = QVBoxLayout(type_group)
        
        self.doc_type_combo = QComboBox()
        self.doc_type_combo.addItems([
            "RESUME",
            "CERTIFICATE",
            "DIPLOMA",
            "ACADEMIC TRANSCRIPT",
            "COVER_LETTER",
            "REFERENCE_LETTER",
            "PORTFOLIO",
            "WORK_SAMPLES",
            "PROJECT_DOCUMENTATION",
            "TRAINING_CERTIFICATE",
            "PROFESSIONAL_LICENSE",
            "SKILLS_ASSESSMENT",
            "PERFORMANCE_REVIEW",
            "AWARDS_AND_ACHIEVEMENTS",
            "OTHER"
        ])
        
        type_layout.addWidget(self.doc_type_combo)
        layout.addWidget(type_group)
        
        # Buttons
        button_layout = QHBoxLayout()
        self.upload_btn = QPushButton("Upload")
        self.cancel_btn = QPushButton("Cancel")
        
        self.upload_btn.clicked.connect(self.upload_document)
        self.cancel_btn.clicked.connect(self.reject)
        
        button_layout.addWidget(self.upload_btn)
        button_layout.addWidget(self.cancel_btn)
        layout.addLayout(button_layout)
    
    def browse_file(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self, 
            "Select Document", 
            "", 
            "Documents (*.pdf *.txt *.doc *.docx);;All Files (*)"
        )
        if file_path:
            self.file_input.setText(file_path)
    
    def upload_document(self):
        file_path = self.file_input.text().strip()
        if not file_path:
            QMessageBox.warning(self, "Error", "Please select a file.")
            return
        
        if not os.path.exists(file_path):
            QMessageBox.warning(self, "Error", "File does not exist.")
            return
        
        self.file_path = file_path
        self.doc_type = self.doc_type_combo.currentText().upper()
        self.upload_data_ready.emit(self.file_path, self.doc_type)
        self.accept()

class SystemTrayIcon(QSystemTrayIcon):
    """Custom system tray icon with JobOps functionality"""
    
    def __init__(self, app_instance, parent=None):
        super().__init__(parent)
        self.app_instance = app_instance
        self.animation_timer = None
        self.animation_frames = self._create_animation_frames()
        self.animation_index = 0
        self.is_animating = False
        self.progress_dialog = None
        self._workers = set()  # Keep references to running workers
        self.setup_tray()
    
    def _create_animation_frames(self):
        """Create a list of QIcons for animation (spinner effect)"""
        frames = []
        base_pixmap = ResourceManager.create_app_icon().pixmap(64, 64)
        for angle in range(0, 360, 30):
            pixmap = QPixmap(base_pixmap.size())
            pixmap.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pixmap)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.translate(pixmap.width() / 2, pixmap.height() / 2)
            painter.rotate(angle)
            painter.translate(-pixmap.width() / 2, -pixmap.height() / 2)
            painter.drawPixmap(0, 0, base_pixmap)
            painter.end()
            frames.append(QIcon(pixmap))
        return frames if frames else [ResourceManager.create_app_icon()]

    def start_animation(self):
        if self.is_animating:
            return
        logging.info("Starting tray icon animation (generation in progress)")
        self.is_animating = True
        if not self.animation_timer:
            self.animation_timer = QTimer(self)
            self.animation_timer.timeout.connect(self._animate_icon)
        self.animation_index = 0
        self.animation_timer.start(100)  # 100ms per frame

    def stop_animation(self):
        if self.animation_timer:
            self.animation_timer.stop()
        self.setIcon(ResourceManager.create_app_icon())
        self.is_animating = False
        logging.info("Stopped tray icon animation (generation finished)")

    def _animate_icon(self):
        if not self.is_animating or not self.animation_frames:
            return
        self.setIcon(self.animation_frames[self.animation_index])
        self.animation_index = (self.animation_index + 1) % len(self.animation_frames)

    def setup_tray(self):
        # Set icon
        self.setIcon(ResourceManager.create_app_icon())
        self.setToolTip("JobOps - AI Motivation Letter Generator")
        
        # Create context menu
        menu = QMenu()
        
        # Add actions
        upload_action = QAction("📁 Upload Document", self)
        generate_action = QAction("✨ Generate Letter", self)
        archive_action = QAction("💾 View Archive", self)
        log_viewer_action = QAction("📝 View Logs", self)
        settings_action = QAction("⚙️ Settings", self)
        help_action = QAction("❓ Help", self)
        quit_action = QAction("❌ Exit", self)
        
        # Connect actions
        upload_action.triggered.connect(self.upload_document)
        generate_action.triggered.connect(self.generate_letter)
        archive_action.triggered.connect(self.show_archive)
        log_viewer_action.triggered.connect(self.show_log_viewer)
        settings_action.triggered.connect(self.show_settings)
        help_action.triggered.connect(self.show_help)
        quit_action.triggered.connect(self.quit_application)
        
        # Add to menu
        menu.addAction(upload_action)
        menu.addAction(generate_action)
        menu.addSeparator()
        menu.addAction(archive_action)
        menu.addAction(log_viewer_action)
        menu.addAction(settings_action)
        menu.addAction(help_action)
        menu.addSeparator()
        menu.addAction(quit_action)
        
        self.setContextMenu(menu)
        
        # Connect signals
        self.messageClicked.connect(self.on_message_clicked)
        self.activated.connect(self.on_tray_activated)
    
    def upload_document(self):
        logging.info("User triggered: Upload Document dialog")
        dialog = UploadDialog()
        dialog.upload_data_ready.connect(self._start_upload_worker)
        dialog.exec()
    
    def _start_upload_worker(self, file_path, doc_type):
        logging.info(f"Uploading document: {file_path} as {doc_type}")
        worker = UploadWorker(self.app_instance, file_path, doc_type)
        self._workers.add(worker)
        worker.finished.connect(lambda msg, w=worker: self._on_worker_done(w, msg, is_error=False))
        worker.error.connect(lambda err, w=worker: self._on_worker_done(w, err, is_error=True))
        worker.start()
    
    def generate_letter(self):
        logging.info("User triggered: Generate Letter dialog")
        dialog = JobInputDialog(self.app_instance)
        dialog.job_data_ready.connect(self._start_generate_worker)
        dialog.exec()
    
    def _start_generate_worker(self, job_data):
        logging.info(f"Starting letter generation for job data: {job_data}")
        self.generate_worker = GenerateWorker(self.app_instance, job_data)
        self.generate_worker.finished.connect(self.on_generation_finished)
        self.generate_worker.error.connect(self.on_generation_error)
        self.start_animation()
        # Show progress dialog
        self.progress_dialog = QProgressDialog("Generating motivation letter...", None, 0, 0)
        self.progress_dialog.setWindowTitle("JobOps")
        self.progress_dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.progress_dialog.setCancelButton(None)
        self.progress_dialog.setMinimumDuration(0)
        self.progress_dialog.show()
        # Show notification
        self.showMessage(
            "JobOps",
            "Generating your motivation letter...",
            QSystemTrayIcon.MessageIcon.Information,
            2000
        )
        self.generate_worker.start()
    
    def show_archive(self):
        logging.info("User triggered: Show Archive dialog")
        # Open ~/.jobops/motivations/
        motivations_dir = os.path.expanduser("~/.jobops/motivations")
        if os.path.exists(motivations_dir):
            subprocess.Popen(['xdg-open', motivations_dir])
        else:
            logging.error("Motivations directory does not exist.")
            QMessageBox.warning(None, "Archive", "Motivations directory does not exist.")

    
    def show_settings(self):
        logging.info("User triggered: Settings dialog")
        # Open config.json in the default text editor
        config_path = str(getattr(self.app_instance, 'config_path', Path.home() / ".jobops" / "config.json"))
        try:
            if sys.platform.startswith('win'):
                os.startfile(config_path)
            elif sys.platform.startswith('darwin'):
                subprocess.Popen(['open', config_path])
            else:
                subprocess.Popen(['xdg-open', config_path])
        except Exception as e:
            logging.error(f"Failed to open config.json: {e}")
            QMessageBox.warning(None, "Settings", f"Could not open config.json: {e}")
    
    def show_help(self):
        documentation_url = "https://github.com/codesapienbe/jobops-toolbar"
        log_message = f"User triggered: Help/documentation, opening {documentation_url}"
        logging.info(log_message)
        self.showMessage("JobOps", log_message, QSystemTrayIcon.MessageIcon.Information, 3000)
        webbrowser.open(documentation_url)
    
    def quit_application(self):
        log_message = "User triggered: Quit application"
        logging.info(log_message)
        self.showMessage("JobOps", log_message, QSystemTrayIcon.MessageIcon.Information, 3000)
        reply = QMessageBox.question(
            None, 
            "Exit JobOps", 
            "Are you sure you want to exit JobOps?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        
        if reply == QMessageBox.StandardButton.Yes:
            QApplication.quit()
    
    def restart_application(self):
        log_message = "User triggered: Restart application"
        logging.info(log_message)
        self.showMessage("JobOps", log_message, QSystemTrayIcon.MessageIcon.Information, 3000)
        # make sure that the application is restarted
        exit_thread = threading.Thread(target=QApplication.quit)
        exit_thread.start()
        exit_thread.join()  # Wait for exit to complete
        restart_thread = threading.Thread(target=QApplication.exec)
        restart_thread.start()
        restart_thread.join()

    def on_upload_finished(self, message):
        log_message = f"Upload finished: {message}"
        logging.info(log_message)
        self.showMessage("JobOps", log_message, QSystemTrayIcon.MessageIcon.Information, 3000)
    
    def on_upload_error(self, error):
        log_message = f"Upload error: {error}"
        logging.error(log_message)
        self.showMessage("JobOps Error", log_message, QSystemTrayIcon.MessageIcon.Critical, 5000)
    
    def on_generation_finished(self, message):
        log_message = "Letter generation finished successfully."
        logging.info(log_message)
        self.stop_animation()
        if self.progress_dialog:
            self.progress_dialog.close()
            self.progress_dialog = None
        # Show tray notification
        self.showMessage("JobOps", log_message, QSystemTrayIcon.MessageIcon.Information, 5000)
        logging.info(log_message)
        # Fallback: use NotificationService if available
        if hasattr(self.app_instance, 'notification_service') and self.app_instance.notification_service:
            self.app_instance.notification_service.notify("JobOps", message)
            logging.info("Fallback notification service used.")
        # Show preview dialog with letter content
        self.show_letter_preview()

    def show_letter_preview(self):
        # Fetch the latest generated letter from the repository
        letters = self.app_instance.repository.get_by_type(DocumentType.COVER_LETTER)
        if not letters:
            return
        latest_letter = letters[0]  # Assuming the latest letter is the first one
        content = latest_letter.structured_content or latest_letter.raw_content
        if not content:
            return
        # Create and show preview dialog
        preview_dialog = QDialog()
        preview_dialog.setWindowTitle("Letter Preview")
        preview_dialog.setMinimumSize(800, 600)
        layout = QVBoxLayout(preview_dialog)
        text_edit = QTextEdit()
        text_edit.setPlainText(content)
        text_edit.setReadOnly(True)
        layout.addWidget(text_edit)
        preview_dialog.exec()

    def on_generation_error(self, error):
        log_message = f"Letter generation error: {error}"
        logging.error(log_message)
        self.stop_animation()
        if self.progress_dialog:
            self.progress_dialog.close()
            self.progress_dialog = None
        # Show tray notification
        self.showMessage("JobOps Error", log_message, QSystemTrayIcon.MessageIcon.Critical, 5000)
        logging.info(log_message)
        # Fallback: use NotificationService if available
        if hasattr(self.app_instance, 'notification_service') and self.app_instance.notification_service:
            self.app_instance.notification_service.notify("JobOps Error", log_message)
            logging.info("Fallback notification service used.")
        # Also show error dialog
        QMessageBox.critical(None, "Generation Error", error)
    
    def on_message_clicked(self):
        log_message = "Tray message clicked."
        logging.info(log_message)
        pass
    
    def on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            log_message = "Tray icon double-clicked: opening generate letter dialog."
            logging.info(log_message)
            self.generate_letter()

    def _on_worker_done(self, worker, message, is_error):
        log_message = f"Upload worker done: {message}"
        logging.info(log_message)
        self._workers.discard(worker)
        if is_error:
            self.on_upload_error(message)
        else:
            self.on_upload_finished(message)

    def show_log_viewer(self):
        log_file = str(Path.home() / ".jobops" / "app.log")
        dlg = LogViewerDialog(log_file, parent=None)
        dlg.exec()

class UploadWorker(QThread):
    """Background worker for document upload"""
    finished = Signal(str)
    error = Signal(str)
    
    def __init__(self, app_instance, file_path, doc_type):
        super().__init__()
        self.app_instance = app_instance
        self.file_path = file_path
        self.doc_type = doc_type
    
    def run(self):
        try:
            import os
            import shutil
            from datetime import datetime
            from markitdown import MarkItDown
            from openai import OpenAI
            from jobops.models import Document, DocumentType
            repository = getattr(self.app_instance, 'repository', None)
            span_id = get_span_id()
            logging.info(f"UploadWorker started for file: {self.file_path}, doc_type: {self.doc_type}", extra={"span_id": span_id})
            if repository is None:
                logging.error("Application is missing repository.", extra={"span_id": span_id})
                raise Exception("Application is missing repository.")

            # Copy uploaded file to ~/.jobops/resumes/
            resumes_dir = os.path.expanduser("~/.jobops/resumes")
            os.makedirs(resumes_dir, exist_ok=True)
            dest_path = os.path.join(resumes_dir, os.path.basename(self.file_path))
            shutil.copy2(self.file_path, dest_path)
            logging.info(f"Copied file to: {dest_path}", extra={"span_id": span_id})

            # Use dest_path as the filename for the Document
            filename_for_db = dest_path

            # Determine file extension
            _, ext = os.path.splitext(self.file_path)
            ext = ext.lower()

            if ext == ".md":
                # If markdown, just read the content
                with open(self.file_path, "r", encoding="utf-8") as f:
                    structured_content = f.read()
                raw_content = structured_content
                logging.info(f"Markdown file detected, skipping MarkItDown conversion.", extra={"span_id": span_id})
            else:
                # Get the LLM backend and model
                generator = getattr(self.app_instance, 'generator', None)
                llm_backend = getattr(generator, 'llm_backend', None)
                logging.info(f"Detected backend: {llm_backend}", extra={"span_id": span_id})
                if not llm_backend:
                    logging.error("No LLM backend available for MarkItDown.", extra={"span_id": span_id})
                    raise Exception("No LLM backend available for MarkItDown.")
                backend_name = getattr(llm_backend, 'name', '').lower()
                llm_model = getattr(llm_backend, 'model', 'gpt-4o')
                logging.info(f"Backend name: {backend_name}, model: {llm_model}", extra={"span_id": span_id})
                if backend_name in ("openai", "groq"):
                    llm_client = llm_backend.client
                    logging.info(f"Using OpenAI/Groq client: {type(llm_client)}", extra={"span_id": span_id})
                elif backend_name == "ollama":
                    base_url = getattr(llm_backend, 'base_url', 'http://localhost:11434')
                    logging.info(f"Using Ollama backend, base_url: {base_url}", extra={"span_id": span_id})
                    llm_client = OpenAI(base_url=f"{base_url}/v1", api_key="ollama")
                else:
                    logging.error(f"MarkItDown integration is not yet supported for backend: {backend_name}", extra={"span_id": span_id})
                    raise Exception(f"MarkItDown integration is not yet supported for backend: {backend_name}")
                md = MarkItDown(llm_client=llm_client, llm_model=llm_model)
                logging.info(f"Starting MarkItDown extraction for file: {self.file_path}", extra={"span_id": span_id})
                try:
                    result = md.convert(self.file_path)
                except Exception as e:
                    # Check for MissingDependencyException in the error message
                    if "MissingDependencyException" in str(e) and ("docx" in ext or "docx" in str(e)):
                        msg = (
                            "File conversion failed: MarkItDown requires extra dependencies to read .docx files. "
                            "Please install with: pip install markitdown[docx] or pip install markitdown[all]"
                        )
                        logging.error(msg, extra={"span_id": span_id})
                        self.error.emit(msg)
                        return
                    else:
                        raise
                structured_content = result.text_content
                raw_content = result.raw_text if hasattr(result, 'raw_text') else structured_content
                logging.info(f"Extraction complete, structured length: {len(structured_content)}", extra={"span_id": span_id})

            doc_type_enum = DocumentType.RESUME if self.doc_type.upper() == "RESUME" else DocumentType.CERTIFICATION
            doc = Document(
                type=doc_type_enum,
                filename=filename_for_db,
                raw_content=raw_content,
                structured_content=structured_content,
                uploaded_at=datetime.now()
            )
            repository.save(doc)
            logging.info(f"Document saved to database: {filename_for_db}", extra={"span_id": span_id})
            self.finished.emit(f"Document uploaded and parsed successfully: {os.path.basename(self.file_path)}")
        except Exception as e:
            import traceback
            logging.error(f"Upload failed: {str(e)}\n{traceback.format_exc()}", extra={"span_id": span_id})
            self.error.emit(f"Upload failed: {str(e)}\n{traceback.format_exc()}")

class GenerateWorker(QThread):
    """Background worker for letter generation"""
    finished = Signal(str)
    error = Signal(str)
    
    def __init__(self, app_instance, job_data):
        super().__init__()
        self.app_instance = app_instance
        self.job_data = job_data
    
    def run(self):
        try:
            log_message = "GenerateWorker started."
            logging.info(log_message)
            repository = getattr(self.app_instance, 'repository', None)
            generator = getattr(self.app_instance, 'generator', None)
            if repository is None or generator is None:
                log_message = "Application is missing repository or generator."
                logging.error(log_message)
                raise Exception("Application is missing repository or generator.")
            resume_markdown = repository.get_latest_resume()
            if resume_markdown is None:
                log_message = "No resume found. Please upload your resume first."
                logging.error(log_message)
                raise Exception("No resume found. Please upload your resume first.")
            logging.info(f"Resume markdown (first 100 chars): {resume_markdown[:100]}")
            job_markdown = self.job_data.get('job_markdown', '')
            if not job_markdown:
                raise Exception("Job description markdown is required.")
            detected_language = self.job_data.get('detected_language', 'en')
            url = self.job_data.get('url', '')
            # Get truncation config from app_instance if available
            config = getattr(self.app_instance, '_config', None)
            backend_type = config.get('backend', 'ollama') if config else None
            backend_settings = config.get('backend_settings', {}) if config else {}
            model_conf = backend_settings.get(backend_type, {}) if backend_settings else {}
            trunc_config = model_conf if model_conf else {}
            letter = generator.generate_from_markdown(job_markdown, resume_markdown, detected_language, url=url, config=trunc_config)
            motivations_dir = os.path.expanduser("~/.jobops/motivations")
            base_domain = urlparse(url).netloc
            safe_title = re.sub(r'[^a-zA-Z0-9_\-]', '_', base_domain)
            doc_id = str(uuid.uuid4())
            md_filename = f"{safe_title}_{doc_id}.md"
            md_path = os.path.join(motivations_dir, md_filename)
            with open(md_path, 'w', encoding='utf-8') as f:
                f.write(letter.content)
            doc = Document(
                id=doc_id,
                type=DocumentType.COVER_LETTER,
                filename=md_path,
                raw_content=letter.content,
                structured_content=letter.content,
                uploaded_at=letter.generated_at if hasattr(letter, 'generated_at') else None
            )
            repository.save(doc)
            log_message = "Letter generation, export, and storage completed."
            logging.info(log_message)
            self.finished.emit(f"Motivation letter generated, exported to markdown, and saved to database.")
        except Exception as e:
            log_message = f"Exception in GenerateWorker: {e}"
            logging.error(log_message)
            self.error.emit(str(e))

class LogViewerDialog(QDialog):
    def __init__(self, log_file, parent=None):
        super().__init__(parent)
        self.setWindowTitle("JobOps Log Viewer")
        self.setMinimumSize(900, 600)
        layout = QVBoxLayout(self)
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search logs...")
        self.search_input.textChanged.connect(self.filter_logs)
        layout.addWidget(self.search_input)
        self.table = QTableWidget()
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels(["Timestamp", "Level", "Logger", "Message", "span_id", "trace_id"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        layout.addWidget(self.table)
        self.log_file = log_file
        self.all_logs = []
        self.load_logs()

    def load_logs(self):
        self.all_logs.clear()
        self.table.setRowCount(0)
        if not os.path.exists(self.log_file):
            return
        with open(self.log_file, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    log = json.loads(line)
                except Exception:
                    # Fallback: try to parse as plain text
                    log = {"timestamp": "", "level": "", "logger": "", "message": line, "span_id": "", "trace_id": ""}
                self.all_logs.append(log)
        self.display_logs(self.all_logs)

    def display_logs(self, logs):
        self.table.setRowCount(len(logs))
        for row, log in enumerate(logs):
            ts = log.get("timestamp", log.get("asctime", ""))
            lvl = log.get("level", log.get("levelname", ""))
            logger = log.get("logger", log.get("name", ""))
            msg = log.get("message", "")
            span_id = log.get("span_id", "")
            trace_id = log.get("trace_id", "")
            for col, val in enumerate([ts, lvl, logger, msg, span_id, trace_id]):
                item = QTableWidgetItem(str(val))
                if col == 1:  # Level
                    if lvl == "ERROR":
                        item.setForeground(QColor("red"))
                    elif lvl == "WARNING":
                        item.setForeground(QColor("orange"))
                    elif lvl == "INFO":
                        item.setForeground(QColor("blue"))
                    elif lvl == "DEBUG":
                        item.setForeground(QColor("gray"))
                self.table.setItem(row, col, item)

    def filter_logs(self, text):
        text = text.lower()
        filtered = [log for log in self.all_logs if text in json.dumps(log).lower()]
        self.display_logs(filtered)

class JobOpsQtApplication(QApplication):
    """Main Qt application class"""
    
    def __init__(self, args):
        super().__init__(args)
        
        # Set application properties
        self.setApplicationName("JobOps")
        self.setApplicationVersion("2.0.0")
        self.setOrganizationName("JobOps")
        self.setQuitOnLastWindowClosed(False)  # Keep running in system tray
        
        # Initialize application data
        self.base_dir = Path.home() / ".jobops"
        self.base_dir.mkdir(exist_ok=True)

        # --- Load config and initialize repository and generator ---
        from jobops.repositories import SQLiteDocumentRepository
        from jobops.utils import ConcreteLetterGenerator
        from jobops.clients import LLMBackendFactory
        db_path = str(self.base_dir / "jobops.db")
        self.repository = SQLiteDocumentRepository(db_path)

        # Load config.json
        self.config_path = self.base_dir / "config.json"
        self._init_config_and_generator()
        # ---------------------------------------------------
        
        # Output format and debug level from config
        self.output_format = self._config.get("output_format", "markdown").lower()
        self.debug = self._config.get("debug", False)
        self.setup_logging()
        self.notification_service = NotificationService()
        self.system_tray = None
        
        self.setup_system_tray()
        # Automatically prompt for resume upload if none exists
        if self.repository.get_latest_resume() is None:
            self.system_tray.upload_document()
        # Add config.json watcher
        self.config_watcher = QFileSystemWatcher([str(self.config_path)])
        self.config_watcher.fileChanged.connect(self.on_config_changed)
    
    def _init_config_and_generator(self):
        """Load config and initialize generator and settings."""
        if self.config_path.exists():
            with open(self.config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
        else:
            config = {}
        self._config = config
        backend_type = config.get("backend", "ollama")
        backend_settings = config.get("backend_settings", {})
        app_settings = config.get("app_settings", {})
        tokens = app_settings.get("tokens", {})
        backend_conf = backend_settings.get(backend_type, {})
        from jobops.utils import ConcreteLetterGenerator
        from jobops.clients import LLMBackendFactory
        backend = LLMBackendFactory.create(backend_type, backend_conf, tokens)
        self.generator = ConcreteLetterGenerator(backend)
        # Output format and debug level from config
        self.output_format = app_settings.get("output_format", "markdown").lower()
        self.debug = config.get("debug", False)
        # Health check for model validity
        try:
            if hasattr(backend, 'health_check'):
                ok = backend.health_check()
                if not ok:
                    # Provide model list URL based on backend
                    model_urls = {
                        'groq': 'https://console.groq.com/docs/models',
                        'openai': 'https://platform.openai.com/docs/models',
                        'ollama': 'https://ollama.com/library',
                        'gemini': 'https://ai.google.dev/models',
                    }
                    url = model_urls.get(backend_type, None)
                    msg = f"The selected model for backend '{backend_type}' is not available or invalid."
                    if url:
                        msg += f"\nCheck available models here: {url}"
                        import webbrowser
                        webbrowser.open(url)
                    QMessageBox.warning(None, "Model Health Check Failed", msg)
        except Exception as e:
            logging.warning(f"Model health check failed: {e}")

    def on_config_changed(self, path):
        # QFileSystemWatcher sometimes emits multiple times, so reload defensively
        try:
            self._init_config_and_generator()
            self.setup_logging()
            self.notification_service.notify(
                "JobOps Config Reloaded",
                "Settings have been reloaded from config.json."
            )
            log_message = "Config reloaded from config.json"
            logging.info(log_message)
        except Exception as e:
            log_message = f"Failed to reload config: {e}"
            logging.error(log_message)

    def setup_logging(self):
        """Setup application logging"""
        log_file = self.base_dir / 'app.log'
        log_level = logging.DEBUG if getattr(self, 'debug', False) else logging.INFO
        try:
            from pythonjsonlogger import jsonlogger
            file_handler = logging.FileHandler(log_file, encoding='utf-8')
            json_format = '%(asctime)s %(levelname)s %(name)s %(message)s %(span_id)s %(trace_id)s'
            file_handler.setFormatter(jsonlogger.JsonFormatter(json_format))
        except ImportError:
            # Fallback: custom JSON formatter
            class SimpleJsonFormatter(logging.Formatter):
                def format(self, record):
                    import json
                    log_record = {
                        'timestamp': self.formatTime(record, self.datefmt),
                        'level': record.levelname,
                        'logger': record.name,
                        'message': record.getMessage(),
                        'span_id': getattr(record, 'span_id', None),
                        'trace_id': getattr(record, 'trace_id', None),
                    }
                    return json.dumps(log_record)
            file_handler = logging.FileHandler(log_file, encoding='utf-8')
            file_handler.setFormatter(SimpleJsonFormatter())
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(name)s: %(message)s'))
        root_logger = logging.getLogger()
        root_logger.setLevel(log_level)
        # Remove all handlers first (avoid duplicate logs)
        for h in root_logger.handlers[:]:
            root_logger.removeHandler(h)
        root_logger.addHandler(file_handler)
        root_logger.addHandler(stream_handler)
    
    def setup_system_tray(self):
        """Initialize system tray"""
        if not QSystemTrayIcon.isSystemTrayAvailable():
            QMessageBox.critical(
                None,
                "System Tray",
                "System tray is not available on this system."
            )
            sys.exit(1)
        
        self.system_tray = SystemTrayIcon(self)
        self.notification_service.set_system_tray(self.system_tray)
        self.system_tray.show()
        
        # Show startup notification
        self.system_tray.showMessage(
            "JobOps Started",
            "JobOps is running in the system tray. Right-click the icon to access features.",
            QSystemTrayIcon.MessageIcon.Information,
            3000
        )

def main():
    """Main application entry point"""
    
    # Check platform compatibility
    if not check_platform_compatibility():
        sys.exit(1)
    
    # Create Qt application
    app = JobOpsQtApplication(sys.argv)
    
    # Setup platform-specific features
    if sys.platform.startswith('linux'):
        create_desktop_entry()
    
    # Setup global shortcuts (if available)
    try:
        # Global shortcut for quick letter generation (Ctrl+Alt+J)
        shortcut = QShortcut(QKeySequence("Ctrl+Alt+J"), None)
        shortcut.activated.connect(app.system_tray.generate_letter)
        log_message = "Global shortcut registered: Ctrl+Alt+J"
        logging.info(log_message)
    except Exception as e:
        log_message = f"Failed to setup global shortcut: {e}"
        logging.warning(log_message)
    
    # Show startup message
    log_message = "JobOps Qt application started successfully"
    logging.info(log_message)
    
    # Start the application event loop
    try:
        sys.exit(app.exec())
    except KeyboardInterrupt:
        log_message = "Application interrupted by user"
        logging.info(log_message)
        sys.exit(0)
    except Exception as e:
        log_message = f"Application error: {e}"
        logging.error(log_message)
        
        # Show error dialog
        QMessageBox.critical(
            None,
            "JobOps Error",
            f"An unexpected error occurred:\n\n{str(e)}\n\nPlease check the log file for details."
        )
        sys.exit(1)

if __name__ == "__main__":
    main()
