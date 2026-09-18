from pydantic import BaseModel, Field, StrictInt


class LoadCreate(BaseModel):
    piece_id: str = Field(min_length=1, max_length=128)
    # 严格整数：字符串 "100"、小数 100.0、布尔值一律拒绝，不做隐式转换
    weight_grams: StrictInt


class TransferCreate(BaseModel):
    # 转移目标吊杆编号；与源杆相同、不存在由接口在锁内明确拒绝
    target_batten_id: str = Field(min_length=1, max_length=32)
