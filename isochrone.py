import json
import os

import numpy as np
from osgeo import gdal, osr
from PyQt5.QtCore import QDateTime, QEventLoop, Qt, QTime, QUrl
from PyQt5.QtGui import QColor
from PyQt5.QtNetwork import QNetworkReply, QNetworkRequest
from PyQt5.QtWidgets import QDialog, QMessageBox
from qgis.core import (
    QgsCategorizedSymbolRenderer,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsNetworkAccessManager,
    QgsProject,
    QgsRasterLayer,
    QgsRendererCategory,
    QgsSymbol,
    QgsVectorLayer,
)
from qgis.gui import QgsMapToolEmitPoint
from qgis.PyQt import uic
from qgis.utils import iface


class PointTool(QgsMapToolEmitPoint):
    def __init__(self, canvas):
        super(PointTool, self).__init__(canvas)
        self.canvas = canvas

    def canvasReleaseEvent(self, event):
        # クリックされた場所を地図座標に変換
        point = self.toMapCoordinates(event.pos())
        # イベントからマウスボタンを取得
        button = event.button()
        # 地図座標とマウスボタンを親ウィジェットにシグナルとして送信
        self.canvasClicked.emit(point, button)


def flatten_coordinates(coords):
    """ネストされたGeoJSONの座標を再帰的にフラット化する関数"""
    for coord in coords:
        if isinstance(coord[0], (list, tuple)):
            # ネストされた座標の場合は再帰処理
            yield from flatten_coordinates(coord)
        else:
            # 単一の座標ペア
            yield coord


class Isochrone(QDialog):
    def __init__(self):
        super().__init__()
        self.ui = uic.loadUi(
            os.path.join(os.path.dirname(__file__), "isochrone.ui"), self
        )
        self.canvas = iface.mapCanvas() if iface else self.parent().canvas
        self.pointTool = PointTool(self.canvas)
        self.pointTool.canvasClicked.connect(self.update_position)

        self.ui.pushButton_position.clicked.connect(self.activate_point_tool)
        self.ui.pushButton_run.clicked.connect(self.request_isochrone)
        self.ui.pushButton_cancel.clicked.connect(self.close)
        self.startTime.setDateTime(QDateTime.currentDateTime())
        self.manager = QgsNetworkAccessManager.instance()
        self.setWindowFlags(self.windowFlags() | Qt.WindowStaysOnTopHint)
        self.arrive_boolean = False
        self.geojson_files = []
        # geojsonで拡張子を自動設定
        self.mQgsFileWidget_output.setFilter("*.geojson")

        # デフォルト値を保存するためのチェックボックス
        self.ui.saveAsDefaultCheckBox.stateChanged.connect(self.save_default_settings)
        self.settings_file = os.path.join(
            os.path.expanduser("~"), "isochrone_settings.json"
        )
        self.load_settings()

    def activate_point_tool(self):
        # ダイアログを隠す
        self.hide()
        # ポイントツールをアクティブにする
        self.canvas.setMapTool(self.pointTool)

    # ポイントがクリックされた後の処理を追加
    def update_position(self, point):
        self.show()
        self.raise_()
        self.activateWindow()

        source_crs = self.canvas.mapSettings().destinationCrs()
        epsg4326 = QgsCoordinateReferenceSystem("EPSG:4326")
        transform = QgsCoordinateTransform(source_crs, epsg4326, QgsProject.instance())
        point_transformed = transform.transform(point)
        coord_text = "{:.6f}, {:.6f}".format(
            point_transformed.y(), point_transformed.x()
        )
        self.ui.standardPosition.setText(coord_text)

        # ポイントツールを非アクティブにする
        self.canvas.unsetMapTool(self.pointTool)

    def generate_cutoff_secs(self, max_minutes, interval_minutes):
        max_seconds = max_minutes * 60
        interval_seconds = interval_minutes * 60
        return "&".join(
            f"cutoffSec={sec}" for sec in range(0, max_seconds + 1, interval_seconds)
        )

    def request_isochrone(self):
        lat, lon = self.ui.standardPosition.text().split(",")

        # startTime と finishTime の取得
        start_time = self.ui.startTime.dateTime()
        finish_time = self.ui.finishTime.dateTime()

        # finishTimeが空欄の場合
        if not self.ui.finishTime.dateTime().isValid():
            finish_time = start_time  # 同じ時間に設定してループを1回のみ実行

        # 秒数を "00" にリセット
        start_time.setTime(
            QTime(start_time.time().hour(), start_time.time().minute(), 0)
        )
        finish_time.setTime(
            QTime(finish_time.time().hour(), finish_time.time().minute(), 0)
        )

        # outputTimeInterval（QSpinBox）から間隔を分単位で取得
        time_interval_minutes = self.ui.outputTimeInterval.value()

        cutoff_query = self.generate_cutoff_secs(
            self.ui.maxTime.value(), self.ui.outputPolygonInterval.value()
        )

        # デフォルトの始点と終点を設定
        lat_dep, lon_dep = ("43.0815", "141.3074")
        lat_ari, lon_ari = ("43.0815", "141.3074")
        self.arrive_boolean = False

        # buttonGroupの選択に基づいて始点または終点を更新
        if self.ui.arriveby.checkedButton() == self.ui.button_dep:
            lat_dep, lon_dep = (lat, lon)
            self.arrive_boolean = False
        elif self.ui.arriveby.checkedButton() == self.ui.button_ari:
            lat_ari, lon_ari = (lat, lon)
            self.arrive_boolean = True

        arrive_boolean_str = "true" if self.arrive_boolean else "false"

        # サーバーURLを設定
        server_url = self.ui.serverUrlAreaText.text().strip()
        if server_url.endswith("/"):
            server_url = server_url[:-1]  # 最後の「/」を取り除く

        # start_timeとfinish_timeの間を指定された間隔（分）ごとに処理
        current_time = start_time
        self.error_occurred = False  # エラーフラグの初期化

        while current_time <= finish_time and not self.error_occurred:
            current_date_str = current_time.toString("yyyy-MM-dd")
            current_time_str = current_time.toString("HH:mm:ss")  # 秒数も含める

            full_url = (
                f"{server_url}/otp/routers/default/isochrone"
                f"?fromPlace={lat_dep},{lon_dep}&toPlace={lat_ari},{lon_ari}"
                f"&date={current_date_str}&time={current_time_str}&mode=WALK,TRANSIT"
                f"&arriveBy={arrive_boolean_str}&{cutoff_query}"
            )

            request = QNetworkRequest(QUrl(full_url))
            request.setRawHeader(b"Accept", b"application/json")

            # リクエストを送信して次の時刻に進める
            self.send_request_and_wait(request, current_date_str, current_time_str)

            # エラーが発生した場合は処理を中断
            if self.error_occurred:
                break

            # finishTimeが空欄の場合はループを1回で終了
            if not self.ui.finishTime.dateTime().isValid():
                break

            # 指定された分の間隔で current_time を進める
            current_time = current_time.addSecs(time_interval_minutes * 60)

        # 全てのリクエストが完了した後の処理
        if not self.error_occurred and self.ui.finishTime.dateTime().isValid():
            self.calculate_bounding_box()  # バウンディングボックスを計算
            # ラスタ化が完了した後に統計ラスタを生成
            self.create_statistical_rasters()
            QMessageBox.information(self, "完了", "処理が終了しました")

    def send_request_and_wait(self, request, current_date_str, current_time_str):
        reply = self.manager.get(request)
        loop = QEventLoop()

        # リプライの完了時にループを終了
        reply.finished.connect(loop.quit)
        loop.exec_()

        # エラーがない場合のみレスポンスを処理
        if reply.error() == QNetworkReply.NoError:
            self.handle_response(reply, current_date_str, current_time_str)
        else:
            QMessageBox.critical(
                self, "Error", f"Network error occurred: {reply.errorString()}"
            )
            self.error_occurred = True
        reply.deleteLater()

    def handle_response(self, reply, current_date_str, current_time_str):
        error = reply.error()

        if error == QNetworkReply.NoError:
            content_type = reply.header(QNetworkRequest.ContentTypeHeader)

            if content_type and "application/json" in content_type:
                reply_data = reply.readAll()
                if reply_data:
                    try:
                        geojson_data = json.loads(str(reply_data, "utf-8"))

                        # current_time_str を "HHmm" 形式に変換（ファイル名用に秒を省略）
                        time_str_for_file = current_time_str[:5].replace(
                            ":", ""
                        )  # HHmm

                        # ファイルを保存し、保存されたファイルパスを取得
                        file_path = self.save_isochrone_to_directory(
                            geojson_data,
                            current_date_str.replace("-", ""),
                            time_str_for_file,
                        )
                        if file_path:
                            self.geojson_files.append(file_path)  # ファイルパスを保存
                    except json.JSONDecodeError as e:
                        QMessageBox.critical(
                            self, "Error", f"JSON decode error: {str(e)}"
                        )
                        self.error_occurred = True
                else:
                    QMessageBox.critical(self, "Error", "Empty response received")
                    self.error_occurred = True
            else:
                QMessageBox.critical(
                    self, "Error", "Unsupported or missing content type"
                )
                self.error_occurred = True
        else:
            QMessageBox.critical(
                self, "Error", f"Network error occurred: {reply.errorString()}"
            )
            self.error_occurred = True

        reply.deleteLater()

    def rasterize_all_geojson_files(self):
        # メッシュのGeoJSONファイルのパス
        arrive_or_departure = "arrive" if self.arrive_boolean else "departure"
        start_time_str = self.ui.startTime.dateTime().toString("yyyyMMddHHmm")
        finish_time_str = self.ui.finishTime.dateTime().toString("yyyyMMddHHmm")

        # ファイル名の構築
        grid_geojson_filename = (
            f"grid_{arrive_or_departure}_{start_time_str}_{finish_time_str}.geojson"
        )

        # パスの結合
        grid_geojson_path = os.path.join(
            self.mQgsFileWidget_output.filePath(), grid_geojson_filename
        )

        # メッシュデータをロード
        mesh_layer = QgsVectorLayer(grid_geojson_path, "mesh", "ogr")

        if not mesh_layer.isValid():
            QMessageBox.critical(
                self, "Layer Error", "メッシュデータのロードに失敗しました"
            )
            return

        # メッシュの範囲と解像度を取得
        extent = mesh_layer.extent()
        x_min = extent.xMinimum()
        x_max = extent.xMaximum()
        y_min = extent.yMinimum()
        y_max = extent.yMaximum()

        # メッシュの解像度を取得
        mesh_feature = next(mesh_layer.getFeatures())
        geom = mesh_feature.geometry()
        pixel_size = geom.boundingBox().width()

        cols = int((x_max - x_min) / pixel_size)
        rows = int((y_max - y_min) / pixel_size)

        # CRSをEPSG:4326に固定
        epsg4326 = osr.SpatialReference()
        epsg4326.ImportFromEPSG(4326)

        # GeoTIFF用のディレクトリを作成
        base_path = self.mQgsFileWidget_output.filePath()
        geotiff_dir = os.path.join(base_path, "data_geotiff")
        os.makedirs(geotiff_dir, exist_ok=True)

        # 各GeoJSONファイルのラスタ化
        for geojson_file in self.geojson_files:
            # 出力ファイル名を生成
            output_raster_path = os.path.join(
                geotiff_dir,
                os.path.basename(geojson_file).replace(".geojson", ".geotiff"),
            )
            # ラスタの作成
            driver = gdal.GetDriverByName("GTiff")
            target_ds = driver.Create(
                output_raster_path, cols, rows, 1, gdal.GDT_Float32
            )
            target_ds.SetGeoTransform((x_min, pixel_size, 0, y_max, 0, -pixel_size))

            # CRSをEPSG:4326に設定
            target_ds.SetProjection(epsg4326.ExportToWkt())

            # NoData値を設定
            band = target_ds.GetRasterBand(1)
            band.SetNoDataValue(np.nan)

            # GeoJSONファイルを読み込み
            shp_ds = gdal.OpenEx(geojson_file, gdal.OF_VECTOR)
            if shp_ds is None:
                QMessageBox.critical(
                    self,
                    "Error",
                    f"GeoJSONファイルの読み込みに失敗しました: {geojson_file}",
                )
                continue

            # ラスタ化
            layer = shp_ds.GetLayer()
            gdal.RasterizeLayer(
                target_ds, [1], layer, options=["ATTRIBUTE=time", "ALL_TOUCHED=TRUE"]
            )

            # データを処理して保存
            raster_data = band.ReadAsArray()
            raster_data[raster_data == 0] = np.nan
            band.WriteArray(raster_data)

            # 終了処理
            target_ds = None
            shp_ds = None

            print(f"ラスタ化が完了しました: {output_raster_path}")

    def create_statistical_rasters(self):
        # GeoTIFFファイルが保存されているディレクトリを取得
        base_path = self.mQgsFileWidget_output.filePath()
        geotiff_dir = os.path.join(base_path, "data_geotiff")

        # すべてのGeoTIFFファイルをリストとして取得
        geotiff_files = [
            os.path.join(
                geotiff_dir, os.path.basename(f).replace(".geojson", ".geotiff")
            )
            for f in self.geojson_files
        ]

        # ラスタデータを読み込み、行列サイズの取得
        ds = gdal.Open(geotiff_files[0])
        band = ds.GetRasterBand(1)
        rows, cols = band.ReadAsArray().shape

        # 統計ラスタの初期化（全てNaN）
        min_data = np.full((rows, cols), np.nan)
        max_data = np.full((rows, cols), np.nan)
        median_data = np.full((rows, cols), np.nan)
        mean_data = np.full((rows, cols), np.nan)

        # すべてのファイルのタイルを走査して、最小値、最大値、中央値、平均を計算
        for i in range(rows):
            for j in range(cols):
                # メッシュに対して有効な（NaN以外の）値を集める
                values = []
                for geotiff_file in geotiff_files:
                    ds = gdal.Open(geotiff_file)
                    band = ds.GetRasterBand(1)
                    value = band.ReadAsArray()[i, j]
                    if not np.isnan(value):
                        values.append(value)

                if values:  # 1つ以上の有効な値があれば統計処理
                    values = np.array(values)
                    min_data[i, j] = np.min(values)
                    max_data[i, j] = np.max(values)
                    median_data[i, j] = np.median(values)
                    mean_data[i, j] = np.mean(values)

        # 差分の計算 (最大値 - 最小値)
        diff_data = max_data - min_data

        # 統計結果を保存
        self.save_raster(min_data, "min")
        self.save_raster(max_data, "max")
        self.save_raster(median_data, "median")
        self.save_raster(mean_data, "mean")
        self.save_raster(diff_data, "diff")

    def save_raster(self, data, stat_type):
        # メッシュの範囲と解像度を再取得
        base_filename = (
            f"grid_{'arrive' if self.arrive_boolean else 'departure'}_"
            f"{self.ui.startTime.dateTime().toString('yyyyMMddHHmm')}_"
            f"{self.ui.finishTime.dateTime().toString('yyyyMMddHHmm')}.geojson"
        )
        grid_geojson_path = os.path.join(
            self.mQgsFileWidget_output.filePath(),
            base_filename,
        )

        mesh_layer = QgsVectorLayer(grid_geojson_path, "mesh", "ogr")

        extent = mesh_layer.extent()
        x_min = extent.xMinimum()
        x_max = extent.xMaximum()
        y_min = extent.yMinimum()
        y_max = extent.yMaximum()

        mesh_feature = next(mesh_layer.getFeatures())
        geom = mesh_feature.geometry()
        pixel_size = geom.boundingBox().width()

        cols = int((x_max - x_min) / pixel_size)
        rows = int((y_max - y_min) / pixel_size)

        # 出力ファイル名を生成
        time_str_start = self.ui.startTime.dateTime().toString("yyyyMMddHHmm")
        time_str_finish = self.ui.finishTime.dateTime().toString("yyyyMMddHHmm")
        stat_file_path = os.path.join(
            self.mQgsFileWidget_output.filePath(),
            f"{stat_type}_{time_str_start}_{time_str_finish}.geotiff",
        )

        # CRSをEPSG:4326に設定
        epsg4326 = osr.SpatialReference()
        epsg4326.ImportFromEPSG(4326)

        # 新しいラスタを作成
        driver = gdal.GetDriverByName("GTiff")
        target_ds = driver.Create(stat_file_path, cols, rows, 1, gdal.GDT_Float32)
        target_ds.SetGeoTransform((x_min, pixel_size, 0, y_max, 0, -pixel_size))
        target_ds.SetProjection(epsg4326.ExportToWkt())

        # データを書き込み
        band = target_ds.GetRasterBand(1)
        band.SetNoDataValue(np.nan)
        band.WriteArray(data)

        # 終了処理
        target_ds = None

        print(f"{stat_type}ラスタが作成されました: {stat_file_path}")

        # QGISにラスタを追加
        raster_layer = QgsRasterLayer(stat_file_path, f"{stat_type}_raster")
        if raster_layer.isValid():
            QgsProject.instance().addMapLayer(raster_layer)
        else:
            QMessageBox.critical(
                self, "Layer Error", f"Failed to load {stat_type} raster layer"
            )

    def calculate_bounding_box(self):
        # バウンディングボックスの初期化
        min_lon, min_lat = float("inf"), float("inf")
        max_lon, max_lat = float("-inf"), float("-inf")

        # すべての保存されたGeoJSONファイルを読み込み、座標範囲を計算
        for file_path in self.geojson_files:
            with open(file_path, "r") as file:
                geojson_data = json.load(file)

                for feature in geojson_data["features"]:
                    geometry = feature.get("geometry")

                    # geometryとcoordinatesの存在を確認
                    if geometry is not None and "coordinates" in geometry:
                        coords = geometry["coordinates"]
                        flat_coords = flatten_coordinates(
                            coords
                        )  # フラット化した座標を取得

                        for lon, lat in flat_coords:
                            min_lon = min(min_lon, lon)
                            min_lat = min(min_lat, lat)
                            max_lon = max(max_lon, lon)
                            max_lat = max(max_lat, lat)

        # バウンディングボックスが有効なら保存
        if min_lon != float("inf") and max_lon != float("-inf"):
            self.create_bounding_box_layer(min_lon, min_lat, max_lon, max_lat)

            # バウンディングボックスをもとにグリッドを作成して保存
            self.save_grid_geojson(min_lon, min_lat, max_lon, max_lat)

        # グリッド作成
        self.save_grid_geojson(min_lon, min_lat, max_lon, max_lat)

        # すべてのGeoJSONファイルをラスタ化
        self.rasterize_all_geojson_files()

    def create_bounding_box_layer(self, min_lon, min_lat, max_lon, max_lat):
        bbox_coords = [
            [min_lon, max_lat],
            [max_lon, max_lat],
            [max_lon, min_lat],
            [min_lon, min_lat],
            [min_lon, max_lat],
        ]

        bbox_geojson = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": {"type": "Polygon", "coordinates": [bbox_coords]},
                    "properties": {},
                }
            ],
        }

        # boundingbox を一時レイヤーとしてロード（ただし QGIS に追加しない）
        self.bbox_layer = QgsVectorLayer(
            json.dumps(bbox_geojson), "temporary_bbox", "ogr"
        )

        if not self.bbox_layer.isValid():
            QMessageBox.critical(
                self, "Layer Error", "Failed to create bounding box layer"
            )
        else:
            # QGIS に追加しないのでメモリ内に一時的に保持
            pass  # 保存も追加も行わない

    def save_grid_geojson(self, min_lon, min_lat, max_lon, max_lat):
        # rasterSizeから間隔（単位）を取得
        grid_size = self.ui.rasterSize.value()

        # min_lon, min_lat から max_lon, max_lat までの範囲でグリッドを作成
        grid_features = []

        current_lon = min_lon
        while current_lon < max_lon:
            current_lat = min_lat
            while current_lat < max_lat:
                # 各グリッドセルの座標を計算
                top_left = [current_lon, current_lat + grid_size]
                top_right = [current_lon + grid_size, current_lat + grid_size]
                bottom_right = [current_lon + grid_size, current_lat]
                bottom_left = [current_lon, current_lat]

                # グリッドセルを長方形のPolygonとしてGeoJSONに追加
                grid_features.append(
                    {
                        "type": "Feature",
                        "geometry": {
                            "type": "Polygon",
                            "coordinates": [
                                [
                                    top_left,
                                    top_right,
                                    bottom_right,
                                    bottom_left,
                                    top_left,
                                ]
                            ],
                        },
                        "properties": {},
                    }
                )

                # 次のセルに移動
                current_lat += grid_size
            current_lon += grid_size

        # GeoJSON構造を作成
        grid_geojson = {"type": "FeatureCollection", "features": grid_features}

        # startTime と finishTime を YYYYMMDDHHmm 形式で取得
        start_date_str = self.ui.startTime.dateTime().toString("yyyyMMddHHmm")
        finish_date_str = self.ui.finishTime.dateTime().toString("yyyyMMddHHmm")
        arrive = "arrive" if self.arrive_boolean else "departure"

        # ファイル名を grid{arrive}{startTime}_{finishTime}.geojson に変更
        file_path = self.mQgsFileWidget_output.filePath()
        grid_file_name = os.path.join(
            file_path, f"grid_{arrive}_{start_date_str}_{finish_date_str}.geojson"
        )

        # ファイルに保存
        with open(grid_file_name, "w") as grid_file:
            json.dump(grid_geojson, grid_file)

        # CRSを明示的に設定するためのフィールドを追加（GeoJSONでは対応できない場合があるため、CRS指定を別途管理）
        print(f"グリッドが作成されました。ファイルパス: {grid_file_name}")

    def apply_categorized_symbology(self, vector_layer, field_name):
        # カテゴリ値と色のリストを準備
        unique_times = sorted([f[field_name] for f in vector_layer.getFeatures()])
        unique_colors = self.generate_colors(len(unique_times))

        # カテゴリシンボルとシンボルレンダラーの準備
        categories = []
        for time, color in zip(unique_times, unique_colors):
            symbol = QgsSymbol.defaultSymbol(vector_layer.geometryType())
            symbol.setColor(QColor(color))
            category = QgsRendererCategory(time, symbol, str(time))
            categories.append(category)

        # カテゴリ別レンダラーを作成
        renderer = QgsCategorizedSymbolRenderer(field_name, categories)
        vector_layer.setRenderer(renderer)
        vector_layer.triggerRepaint()

    def generate_colors(self, num_colors):
        # HSV値を定義（開始、25%、50%、75%、100%）
        hues = [359, 29, 60, 112, 203]
        saturations = [88, 62, 25, 26, 77]
        values = [84, 99, 100, 87, 73]

        # 生成する色の数に応じたHSV値を線形補間で計算
        colors = []
        for i in range(num_colors):
            # 割合を計算
            ratio = i / (num_colors - 1)

            # 割合に応じた色相、彩度、明度を計算
            if ratio <= 0.25:
                # 0%から25%の間
                interp = ratio / 0.25
                hue = (
                    hues[0] + (hues[1] - hues[0] + 360) % 360 * interp
                ) % 360  # 循環的な色相の調整
                saturation = saturations[0] + (saturations[1] - saturations[0]) * interp
                value = values[0] + (values[1] - values[0]) * interp
            elif ratio <= 0.50:
                # 25%から50%の間
                interp = (ratio - 0.25) / 0.25
                hue = hues[1] + (hues[2] - hues[1]) * interp
                saturation = saturations[1] + (saturations[2] - saturations[1]) * interp
                value = values[1] + (values[2] - values[1]) * interp
            elif ratio <= 0.75:
                # 50%から75%の間
                interp = (ratio - 0.50) / 0.25
                hue = hues[2] + (hues[3] - hues[2]) * interp
                saturation = saturations[2] + (saturations[3] - saturations[2]) * interp
                value = values[2] + (values[3] - values[2]) * interp
            else:
                # 75%から100%の間
                interp = (ratio - 0.75) / 0.25
                hue = hues[3] + (hues[4] - hues[3]) * interp
                saturation = saturations[3] + (saturations[4] - saturations[3]) * interp
                value = values[3] + (values[4] - values[3]) * interp

            # QColorオブジェクトを生成し、リストに追加
            colors.append(
                QColor.fromHsv(
                    int(hue), int(saturation * 2.55), int(value * 2.55)
                ).name()
            )

        return colors

    def setup_default_file_name(self, file_date, time_str):
        time_str = time_str[:4]  # 秒をカット
        arrive = "arrive" if self.arrive_boolean else "departure"
        return f"{arrive}_{file_date}{time_str}.geojson"

    def save_isochrone_to_directory(self, geojson_data, file_date, time_str):
        file_name = self.setup_default_file_name(
            file_date, time_str
        )  # ファイル名に日付と時間を含める
        base_path = self.mQgsFileWidget_output.filePath()

        # base_pathが空欄かどうか確認
        if not base_path:
            QMessageBox.critical(
                self,
                "Error",
                "保存先ディレクトリが指定されていません。処理を中断します。",
            )
            self.error_occurred = True  # エラーフラグを設定
            return None

        # 書き込み可能か確認
        if not os.access(base_path, os.W_OK):
            QMessageBox.critical(
                self,
                "Error",
                f"指定されたディレクトリに書き込むことができません: {base_path}。処理を中断します。",
            )
            self.error_occurred = True  # エラーフラグを設定
            return None

        # GeoJSON用のディレクトリを作成
        geojson_dir = os.path.join(base_path, "data_geojson")
        try:
            os.makedirs(geojson_dir, exist_ok=True)
        except OSError as e:
            QMessageBox.critical(
                self,
                "Error",
                f"ディレクトリの作成に失敗しました: {str(e)}。処理を中断します。",
            )
            self.error_occurred = True  # エラーフラグを設定
            return None

        file_path = os.path.join(geojson_dir, file_name)
        with open(file_path, "w") as file:
            file.write(json.dumps(geojson_data))

        self.load_isochrone_to_qgis(file_path)
        return file_path  # 保存されたファイルパスを返す

    def load_isochrone_to_qgis(self, file_path):
        # ファイルパスを使用してレイヤーをロードし、QGISに追加
        vector_layer = QgsVectorLayer(file_path, os.path.basename(file_path), "ogr")

        if vector_layer.isValid():
            QgsProject.instance().addMapLayer(vector_layer)
            self.apply_categorized_symbology(vector_layer, "time")
        else:
            QMessageBox.critical(self, "Layer Error", "Failed to load layer")

    def load_settings(self):
        if os.path.exists(self.settings_file):
            with open(self.settings_file, "r") as file:
                self.default_settings = json.load(file)
                self.ui.standardPosition.setText(
                    self.default_settings.get("position", "")
                )
        else:
            self.default_settings = {}

    def save_default_settings(self):
        if self.ui.saveAsDefaultCheckBox.isChecked():
            self.default_settings["position"] = self.ui.standardPosition.text()
            with open(self.settings_file, "w") as file:
                json.dump(self.default_settings, file)
