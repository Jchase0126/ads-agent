"""结果页数据标识回归（需要 PySide6，不需要 ADS）。

结果页最多保存约 600 个显示点（保极值降采样），指标用的是完整数据。
查看/复制数据时必须明确区分"显示采样点"与"完整数据"，否则用户会把
显示点当成原始数据写进报告（2026-10-02 优化轮点名的问题）。

覆盖：
* 降采样曲线：对话框顶部标识"当前为显示采样点"，表格只有显示点数，
  CSV 复制头部带同样标识；
* 完整曲线（未降采样）：标识"当前为完整数据"；
* 曲线选择器显示 显示点数/完整点数 对比；
* 提供导出回调时出现「导出完整数据」按钮，点击把 (job, expr) 交给回调；
* 无导出回调时不显示按钮（旧调用方式兼容）。

运行（需要一个装了 PySide6 的解释器）::

    python tests/test_result_page_labeling.py
"""

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import add_path, contains, eq, ne, not_contains, ok, run  # noqa: E402

ADDON = add_path("addon", "ads_agent")
BACKEND = add_path("backend")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication
except ImportError as _e:  # pragma: no cover
    print(f"需要 PySide6 才能运行本测试：{_e}")
    print("  pip install PySide6")
    sys.exit(2)

import result_page as rp  # noqa: E402

_APP = QApplication.instance() or QApplication([])

_TMP = tempfile.mkdtemp(prefix="ads_agent_rp_")

_JOB_ID = "dj_rp_label"


def _job(traces_override=None):
    xs = [2.0 + i * 0.001 for i in range(5000)]
    ys = [16.0] * len(xs)
    base = {
        "x_name": "freq", "x_unit": "Hz", "y_name": "dB(S(2,1))",
        "y_unit": "dB", "n_points": 5000, "n_points_raw": 5000,
        "truncated": False, "source": "E:/ws/sim/Amp.ds",
        "n_display": 600, "display_method": "minmax-bucket",
        "quality": {"n_raw": 5000, "n_valid": 5000, "n_dropped": 0,
                    "display_downsampled": True, "display_n": 600,
                    "display_method": "minmax-bucket", "dropped_x": []},
        "x": xs[:600], "y": ys[:600],
    }
    traces = {"dB(S(2,1))": base}
    traces.update(traces_override or {})
    return {"job_id": _JOB_ID, "title": "T",
            "artifacts": {"traces": traces,
                          "dataset_source": "E:/ws/sim/Amp.ds"}}


def test_downsampled_trace_is_labeled_as_display_points():
    dlg = rp.PointsDialog(_job())
    try:
        ok(dlg.badge.text().startswith("⚠"), "降采样曲线要有醒目标识")
        contains(dlg.badge.text(), "显示采样点")
        contains(dlg.badge.text(), "600 / 5000")
        contains(dlg.badge.text(), "minmax-bucket", "要写明降采样算法")
        eq(dlg.table.rowCount(), 600, "表格里是显示点数，不是完整点数")
        contains(dlg.source.text(), "E:/ws/sim/Amp.ds")
        # 复制 CSV：头部带标识
        dlg._copy()
        from PySide6.QtWidgets import QApplication
        text = QApplication.clipboard().text()
        contains(text, "显示采样点")
        contains(text, "600")
        contains(text, "来源: E:/ws/sim/Amp.ds")
        contains(dlg.hint.text(), "显示采样点")
    finally:
        dlg.close()


def test_full_trace_is_labeled_as_complete():
    t = {"x": [1.0, 2.0, 3.0], "y": [10.0, 20.0, 30.0],
         "n_points": 3, "n_points_raw": 3, "n_display": 3,
         "display_method": "none", "truncated": False,
         "source": "E:/ws/sim/B.ds",
         "quality": {"n_raw": 3, "n_valid": 3, "n_dropped": 0, "dropped_x": []}}
    dlg = rp.PointsDialog(_job({"dB(S(2,1))": t}))
    try:
        ok(dlg.badge.text().startswith("✔"), "完整数据打绿勾")
        contains(dlg.badge.text(), "完整数据")
        not_contains(dlg.badge.text(), "显示采样点")
        eq(dlg.table.rowCount(), 3)
        dlg._copy()
        from PySide6.QtWidgets import QApplication
        text = QApplication.clipboard().text()
        contains(text, "完整数据")
        not_contains(text, "显示采样点")
    finally:
        dlg.close()


def test_dropped_points_are_reported():
    t = {"x": [1.0, 2.0], "y": [10.0, 20.0],
         "n_points": 2, "n_points_raw": 4, "n_display": 2,
         "display_method": "none",
         "quality": {"n_raw": 4, "n_valid": 2, "n_dropped": 2,
                     "dropped_x": [1.5, 1.6]}}
    dlg = rp.PointsDialog(_job({"dB(S(2,1))": t}))
    try:
        contains(dlg.badge.text(), "2 点无效已剔除", "丢弃原因要点名数量")
        contains(dlg.source.text(), "读不出丢弃 2 点")
    finally:
        dlg.close()


def test_export_button_calls_back_with_job_and_expr():
    got = []

    def _export(job, expr):
        got.append((job.get("job_id"), expr))

    dlg = rp.PointsDialog(_job(), on_export_full=_export)
    try:
        ok(hasattr(dlg, "export_btn"), "有导出回调时应提供导出按钮")
        dlg.export_btn.click()
        eq(got, [(_JOB_ID, "dB(S(2,1))")])
    finally:
        dlg.close()


def test_no_export_callback_means_no_button():
    dlg = rp.PointsDialog(_job())
    try:
        ok(not hasattr(dlg, "export_btn"), "旧调用方式下不出导出按钮（兼容）")
    finally:
        dlg.close()


def test_strip_traces_keeps_source_and_display_info():
    """持久化层的曲线必须带上标识字段（design_service._strip_traces）。"""
    import design_metrics as dm
    import design_service as dsvc

    # y 用变化值：保极值降采样每桶取 min/max 两点（常量 y 每桶只出 1 点）
    payload = {"x": list(range(2000)), "y": [float(i) for i in range(2000)],
               "x_unit": "Hz", "y_unit": "dB", "n_points": 2000}
    traces = dm.normalize_traces({"t": payload}, max_points=100)
    stripped = dsvc._strip_traces(traces)
    t = stripped["t"]
    eq(t["n_points"], 2000)
    eq(t["n_display"], 100)
    eq(t["display_method"], "minmax-bucket")
    ok("quality" in t)
    ok("source" in t, "来源必须保留")
    eq(len(t["x"]), 100, "持久化的是显示序列")
    eq(t["quality"]["display_downsampled"], True)


if __name__ == "__main__":
    try:
        code = run(globals(), "结果页数据标识")
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    sys.exit(code)
