import tele, tele.meter
from collections import OrderedDict
import torchnet.meter
import os, os.path
import json

class FolderCell(tele.DisplayCell):
  def __init__(self, filename_template):
    super().__init__()
    self.filename_template = filename_template
    self.dir_path = None

  def set_dir(self, dir_path):
    self.dir_path = dir_path
  
  def build_path(self, step_num):
    return os.path.join(self.dir_path, self.filename_template.format(step_num))

class JSONCell(FolderCell):
  def __init__(self, filename_template='metrics_{:04d}.json'):
    super().__init__(filename_template)

  def render(self, step_num, meters):
    path = self.build_path(step_num)
    values = {}
    for meter_name, meter in meters.items():
      values[meter_name] = meter.value()
    with open(path, 'w') as f:
      json.dump(values, f)

class GrowingJSONCell(FolderCell):
  def __init__(self, filename_template='saved_metrics.json'):
    super().__init__(filename_template)

  def render(self, step_num, meters):
    file_path = self.build_path(step_num)
    if os.path.isfile(file_path):
      with open(file_path, 'r') as f:
        values = json.load(f)
    else:
      values = {}
    for meter_name, meter in meters.items():
      if not meter_name in values:
        values[meter_name] = []
      values[meter_name].append(meter.value())
    with open(file_path, 'w') as f:
      json.dump(values, f)

class FolderOutput(tele.TelemetryOutput):
  def __init__(self, dir_path):
    super().__init__()
    self.dir_path = dir_path

  def prepare(self, meters):
    super().prepare(meters)
    os.makedirs(self.dir_path)    
    for i, (meter_names, cell) in enumerate(self.cell_list):
      cell.set_dir(self.dir_path)