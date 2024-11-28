import os

from PyQt5.QtWidgets import QDialog
from qgis.PyQt import uic


class Isochrone(QDialog):
    def __init__(self):
        super().__init__()
        self.ui = uic.loadUi(
            os.path.join(os.path.dirname(__file__), "isochrone.ui"), self
        )
