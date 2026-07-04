from fastapi import FastAPI, Depends
from sqlalchemy.orm import Session
from database import SessionLocal, Transaction # Nhập từ file database.py của bạn

app = FastAPI()

# Hàm hỗ trợ để kết nối với Database
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

@app.get("/")
def home():
    return {"message": "Chào mừng bạn đến với ứng dụng Quản lý Tài chính!"}

# API 1: Lấy danh sách tất cả các khoản chi tiêu (Xem menu)
@app.get("/transactions")
def list_transactions(db: Session = Depends(get_db)):
    transactions = db.query(Transaction).all()
    return transactions

# API 2: Thêm một khoản chi tiêu mới (Đặt món)
@app.post("/transactions")
def create_transaction(amount: float, description: str, category: str, db: Session = Depends(get_db)):
    # Tạo một đối tượng giao dịch mới
    new_item = Transaction(amount=amount, description=description, category=category)
    
    # Lưu vào Database
    db.add(new_item)
    db.commit()
    db.refresh(new_item)
    
    return {"message": "Đã thêm thành công!", "data": new_item}