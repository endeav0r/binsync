import os
import sys

from PyQt5.QtWidgets import QDialog, QVBoxLayout, QLineEdit, QHBoxLayout, QLabel, QPushButton, QGroupBox, \
    QMessageBox, QCheckBox, QWidget, QFileDialog, QGridLayout

import binsync


class ConfigDialog(QDialog):
    def __init__(self, controller, parent=None):
        super(ConfigDialog, self).__init__(parent)

        self._w = None
        self._controller = controller

        self.setWindowTitle("BinSync")

        self._init_widgets()

    def _init_widgets(self):
        self._w = ConfigWidget(self._controller, dialog=self)

        layout = QVBoxLayout()
        layout.addWidget(self._w)

        self.setLayout(layout)


class ConfigWidget(QWidget):
    def __init__(self, controller, dialog):
        super(ConfigWidget, self).__init__()

        self._ssh_agent_edit = None  # type: QLineEdit
        self._user_edit = None  # type: QLineEdit
        self._repo_edit = None  # type: QLineEdit
        self._ssh_auth_sock_edit = None  # type: QLineEdit
        self._controller = controller
        self._dialog = dialog

        # initialization
        self._main_layout = QVBoxLayout()
        self._user_edit = None  # type:QLineEdit
        self._repo_edit = None  # type:QLineEdit
        self._remote_edit = None  # type:QLineEdit
        self._initrepo_checkbox = None  # type:QCheckBox

        self._init_widgets()

        self.setLayout(self._main_layout)

        self.show()

    #
    # Private methods
    #

    def _init_widgets(self):

        upper_layout = QGridLayout()

        # user label
        user_label = QLabel(self)
        user_label.setText("User name")

        self._user_edit = QLineEdit(self)
        self._user_edit.setText("user0_ida")

        row = 0
        upper_layout.addWidget(user_label, row, 0)
        upper_layout.addWidget(self._user_edit, row, 1)
        row += 1

        # binsync label
        binsync_label = QLabel(self)
        binsync_label.setText("Git repo")

        # repo path
        self._repo_edit = QLineEdit(self)
        self._repo_edit.textChanged.connect(self._on_repo_textchanged)
        self._repo_edit.setFixedWidth(150)

        # repo path selection button
        repo_button = QPushButton(self)
        repo_button.setText("...")
        repo_button.clicked.connect(self._on_repo_clicked)
        repo_button.setFixedWidth(40)

        upper_layout.addWidget(binsync_label, row, 0)
        upper_layout.addWidget(self._repo_edit, row, 1)
        upper_layout.addWidget(repo_button, row, 2)
        row += 1

        # clone from a remote URL
        remote_label = QLabel(self)
        remote_label.setText("Remote URL")
        self._remote_edit = QLineEdit(self)
        self._remote_edit.setEnabled(False)

        upper_layout.addWidget(remote_label, row, 0)
        upper_layout.addWidget(self._remote_edit, row, 1)
        row += 1

        # initialize repo checkbox
        self._initrepo_checkbox = QCheckBox(self)
        self._initrepo_checkbox.setText("Create repository")
        self._initrepo_checkbox.setToolTip("I'm the first user of this binsync project and I'd "
                                           "like to initialize it as a sync repo.")
        self._initrepo_checkbox.setChecked(False)
        self._initrepo_checkbox.setEnabled(False)

        upper_layout.addWidget(self._initrepo_checkbox, row, 1)
        row += 1

        # buttons
        self._ok_button = QPushButton(self)
        self._ok_button.setText("OK")
        self._ok_button.setDefault(True)
        self._ok_button.clicked.connect(self._on_ok_clicked)

        cancel_button = QPushButton(self)
        cancel_button.setText("Cancel")
        cancel_button.clicked.connect(self._on_cancel_clicked)

        buttons_layout = QHBoxLayout()
        buttons_layout.addWidget(self._ok_button)
        buttons_layout.addWidget(cancel_button)

        # main layout
        self._main_layout.addLayout(upper_layout)
        self._main_layout.addLayout(buttons_layout)

    #
    # Event handlers
    #

    def _on_ok_clicked(self):
        user = self._user_edit.text()
        path = self._repo_edit.text()
        init_repo = self._initrepo_checkbox.isChecked()

        if not user:
            QMessageBox(self).critical(None, "Invalid user name",
                                       "User name cannot be empty."
                                       )
            return

        if not os.path.isdir(path) and not init_repo:
            QMessageBox(self).critical(None, "Repo does not exist",
                                       "The specified sync directory does not exist. "
                                       "Do you maybe want to initialize it?"
                                       )
            return

        # TODO: Add a user ID to angr management
        if not self.is_git_repo(path):
            remote_url = self._remote_edit.text()
        else:
            remote_url = None

        try:
            self._controller.connect(user, path, init_repo=init_repo, remote_url=remote_url)
        except Exception as e:
            QMessageBox(self).critical(None, "Error connecting to repository", str(e))
            return

        if self._dialog is not None:
            self._dialog.close()
        else:
            self.close()

    def _on_repo_clicked(self):
        d = QFileDialog()
        d.setFileMode(QFileDialog.DirectoryOnly)
        d.exec_()
        dirpath = d.directory().absolutePath()
        if isinstance(dirpath, bytes):
            dirpath = dirpath.decode("utf-8")  # TODO: Use the native encoding on Windows
        self._repo_edit.setText(dirpath)

    def _on_repo_textchanged(self, new_text):
        # is it a git repo?
        if not self.is_git_repo(new_text.strip()):
            # no it's not
            # maybe we want to clone from the remote side?
            self._remote_edit.setEnabled(True)
            self._initrepo_checkbox.setEnabled(True)
        else:
            # yes it is!
            # we don't want to initialize it or allow cloning from the remote side
            self._remote_edit.setEnabled(False)
            self._initrepo_checkbox.setEnabled(False)

    def _on_cancel_clicked(self):
        if self._dialog is not None:
            self._dialog.close()
        else:
            self.close()

    #
    # Static methods
    #

    @staticmethod
    def is_git_repo(path):
        return os.path.isdir(os.path.join(path, ".git"))
