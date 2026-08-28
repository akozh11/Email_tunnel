#!/usr/bin/env python3
"""Графический клиент на PyQt6: написать письмо и отправить в туннель."""

from __future__ import annotations

import sys
from pathlib import Path

from PyQt6.QtCore import QObject, Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from send_client import ClientError, compose_and_send, load_client_config


class SendWorker(QObject):
    finished = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, text: str, images: list[str], subject: str):
        super().__init__()
        self.text = text
        self.images = images
        self.subject = subject

    def run(self) -> None:
        try:
            dest = compose_and_send(self.text, self.images, subject=self.subject)
            self.finished.emit(dest)
        except Exception as exc:
            self.failed.emit(str(exc))


class SendWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Email Tunnel — отправка")
        self.resize(680, 540)
        self.setMinimumSize(560, 440)

        self.images: list[str] = []
        self.thread: QThread | None = None
        self.worker: SendWorker | None = None

        try:
            self.conf = load_client_config()
            self.config_error = ""
        except ClientError as exc:
            self.conf = {
                "client_email": "—",
                "tunnel_email": "—",
                "subject": "Email Tunnel",
            }
            self.config_error = str(exc)

        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(8)

        layout.addWidget(QLabel(f"От:  {self.conf['client_email']}"))
        layout.addWidget(QLabel(f"Кому:  {self.conf['tunnel_email']}"))

        subject_row = QHBoxLayout()
        subject_row.addWidget(QLabel("Тема:"))
        self.subject_edit = QLineEdit(self.conf.get("subject") or "Email Tunnel")
        subject_row.addWidget(self.subject_edit)
        layout.addLayout(subject_row)

        layout.addWidget(QLabel("Текст письма:"))
        self.body = QTextEdit()
        self.body.setPlaceholderText("Напишите письмо сюда...")
        layout.addWidget(self.body, stretch=1)

        attach_row = QHBoxLayout()
        attach_row.addWidget(QLabel("Вложения:"))
        self.attach_label = QLabel("нет")
        self.attach_label.setWordWrap(True)
        attach_row.addWidget(self.attach_label, stretch=1)
        layout.addLayout(attach_row)

        buttons = QHBoxLayout()
        self.add_btn = QPushButton("Приложить фото")
        self.clear_btn = QPushButton("Убрать фото")
        self.send_btn = QPushButton("Отправить")
        self.send_btn.setDefault(True)
        buttons.addWidget(self.add_btn)
        buttons.addWidget(self.clear_btn)
        buttons.addStretch(1)
        buttons.addWidget(self.send_btn)
        layout.addLayout(buttons)

        self.status = QLabel(
            self.config_error or "Напишите письмо и нажмите «Отправить»."
        )
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        self.add_btn.clicked.connect(self.add_images)
        self.clear_btn.clicked.connect(self.clear_images)
        self.send_btn.clicked.connect(self.send_mail)

    def refresh_attach(self) -> None:
        if self.images:
            self.attach_label.setText(", ".join(Path(p).name for p in self.images))
        else:
            self.attach_label.setText("нет")

    def add_images(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "Выберите изображения",
            "",
            "Изображения (*.jpg *.jpeg *.png *.gif *.webp);;Все файлы (*)",
        )
        for path in paths:
            if path not in self.images:
                self.images.append(path)
        self.refresh_attach()

    def clear_images(self) -> None:
        self.images.clear()
        self.refresh_attach()

    def set_busy(self, busy: bool) -> None:
        self.send_btn.setEnabled(not busy)
        self.add_btn.setEnabled(not busy)
        self.clear_btn.setEnabled(not busy)
        self.body.setReadOnly(busy)
        self.subject_edit.setReadOnly(busy)

    def send_mail(self) -> None:
        if self.config_error:
            QMessageBox.critical(self, "Нет настроек", self.config_error)
            return

        text = self.body.toPlainText().strip()
        if not text and not self.images:
            QMessageBox.warning(self, "Пусто", "Введите текст или приложите изображение.")
            return

        self.set_busy(True)
        self.status.setText("Шифрую и отправляю...")

        self.thread = QThread(self)
        self.worker = SendWorker(text, list(self.images), self.subject_edit.text().strip())
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.finished.connect(self.on_ok)
        self.worker.failed.connect(self.on_fail)
        self.worker.finished.connect(self.thread.quit)
        self.worker.failed.connect(self.thread.quit)
        self.thread.finished.connect(self.worker.deleteLater)
        self.thread.start()

    def on_ok(self, dest: str) -> None:
        self.set_busy(False)
        self.status.setText(f"Письмо отправлено на {dest}")
        self.body.clear()
        self.images.clear()
        self.refresh_attach()
        QMessageBox.information(self, "Готово", f"Зашифрованное письмо отправлено на {dest}")

    def on_fail(self, err: str) -> None:
        self.set_busy(False)
        self.status.setText("Ошибка отправки")
        QMessageBox.critical(self, "Ошибка", err)


def start_send_gui() -> None:
    """Открывает окно PyQt, в котором можно написать и отправить письмо."""
    app = QApplication.instance() or QApplication(sys.argv)
    window = SendWindow()
    window.show()
    app.exec()


if __name__ == "__main__":
    start_send_gui()
