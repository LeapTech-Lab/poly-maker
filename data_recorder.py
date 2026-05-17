import csv
import logging
import queue
import threading
from datetime import datetime
from pathlib import Path

LOGGER = logging.getLogger(__name__)

class DataRecorder:
    """在后台线程中将交易周期数据异步写入CSV文件，避免阻塞主循环。"""

    def __init__(self, condition_id: str):
        self.queue = queue.Queue()
        self.condition_id = condition_id
        
        log_dir = Path("logs")
        log_dir.mkdir(exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.filepath = log_dir / f"market_data_{self.condition_id.replace('/', '_')}_{timestamp}.csv"
        
        self.is_running = True
        self.writer_thread = threading.Thread(target=self._write_loop, daemon=True)
        self.writer_thread.start()
        LOGGER.info("DataRecorder started, writing to %s", self.filepath)
        
        self.header_written = False

    def _write_loop(self):
        """从队列中获取数据并写入文件，直到收到停止信号。"""
        while self.is_running or not self.queue.empty():
            try:
                data_dict = self.queue.get(timeout=1)
                if data_dict is None:  # 哨兵值，表示停止
                    continue

                with open(self.filepath, "a", newline="", encoding="utf-8") as f:
                    writer = csv.DictWriter(f, fieldnames=data_dict.keys())
                    if not self.header_written:
                        writer.writeheader()
                        self.header_written = True
                    writer.writerow(data_dict)
                self.queue.task_done()

            except queue.Empty:
                continue # 队列为空时继续等待

    def record(self, data: dict):
        """将一个数据字典放入队列中等待写入。这是一个非阻塞操作。"""
        if self.is_running:
            self.queue.put(data)

    def stop(self):
        """停止记录器，并等待所有缓冲数据写入文件。"""
        LOGGER.info("Stopping DataRecorder, waiting for queue to flush...")
        self.is_running = False
        self.queue.put(None)  # 发送哨兵值以唤醒并最终停止线程
        self.writer_thread.join(timeout=5)
        LOGGER.info("DataRecorder stopped.")