from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
import datetime

# 1. Đường dẫn kết nối Postgres (finance_db là tên DB bạn tạo lúc nãy)
DATABASE_URL = "postgresql://postgres:123456@localhost/finance_db"

# 2. Tạo công cụ kết nối
engine = create_engine(DATABASE_URL)

# 3. Tạo Session để làm việc với DB
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

# 4. Định nghĩa bảng Giao dịch (Transactions)
class Transaction(Base):
    __tablename__ = "transactions"

    id = Column(Integer, primary_key=True, index=True)
    amount = Column(Float, nullable=False)        # Số tiền
    description = Column(String)                  # Ghi chú
    category = Column(String)                     # Danh mục (Ăn uống, Lương...)
    date = Column(DateTime, default=datetime.datetime.utcnow) # Ngày giờ

# 5. Hàm tạo bảng
def init_db():
    Base.metadata.create_all(bind=engine)
    print("--- Đã tạo bảng thành công trong PostgreSQL! ---")

if __name__ == "__main__":
    init_db()