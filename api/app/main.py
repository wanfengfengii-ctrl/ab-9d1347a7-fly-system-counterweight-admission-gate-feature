from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .database import Base, SessionLocal, engine, get_db
from .models import Batten, Load
from .schemas import LoadCreate, TransferCreate

MIN_WEIGHT_GRAMS = 100
MAX_WEIGHT_GRAMS = 25000
FIXTURE_BATTENS = {"G-01": 30000, "G-02": 50000}


def ensure_fixture_battens(db: Session) -> None:
    """保证固定夹具 G-01 / G-02 存在且核定值正确。"""
    for batten_id, capacity in FIXTURE_BATTENS.items():
        batten = db.get(Batten, batten_id)
        if batten is None:
            db.add(Batten(id=batten_id, capacity_grams=capacity))
        elif batten.capacity_grams != capacity:
            batten.capacity_grams = capacity
    db.commit()


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        ensure_fixture_battens(db)
    yield


app = FastAPI(title="剧场吊杆配重装载裁决服务", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    # 转移接口的请求体只有目标吊杆一个参数：缺 body、缺字段、空串、
    # 非字符串等校验失败（loc 落在 body / target_batten_id 上）都说明
    # 转移目标参数不合法，不能套用装载接口的配重标识 / 重量提示
    locs = {loc for err in exc.errors() for loc in err.get("loc", ())}
    if request.url.path.endswith("/transfer") and (
        "target_batten_id" in locs or "body" in locs
    ):
        message = "请求格式不合法：转移目标参数不合法，目标吊杆编号必须是非空字符串"
    else:
        message = "请求格式不合法：配重片标识不能为空，重量必须是整数克数"
    return JSONResponse(
        status_code=422,
        content={
            "accepted": False,
            "reason": "INVALID_INPUT",
            "message": message,
        },
    )


def reject(status_code: int, reason: str, message: str, **extra) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"accepted": False, "reason": reason, "message": message, **extra},
    )


def batten_summary(db: Session, batten: Batten) -> dict:
    total = db.scalar(
        select(func.coalesce(func.sum(Load.weight_grams), 0)).where(
            Load.batten_id == batten.id
        )
    )
    return {
        "batten_id": batten.id,
        "capacity_grams": batten.capacity_grams,
        "total_grams": total,
        "remaining_grams": batten.capacity_grams - total,
    }


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/battens")
def list_battens(db: Session = Depends(get_db)):
    battens = db.scalars(select(Batten).order_by(Batten.id)).all()
    summaries = []
    for batten in battens:
        summary = batten_summary(db, batten)
        summary["load_count"] = db.scalar(
            select(func.count(Load.id)).where(Load.batten_id == batten.id)
        )
        summaries.append(summary)
    return {"battens": summaries}


@app.get("/api/battens/{batten_id}")
def get_batten(batten_id: str, db: Session = Depends(get_db)):
    batten = db.get(Batten, batten_id)
    if batten is None:
        return reject(404, "BATTEN_NOT_FOUND", f"吊杆 {batten_id} 不存在")
    summary = batten_summary(db, batten)
    loads = db.scalars(
        select(Load).where(Load.batten_id == batten_id).order_by(Load.id)
    ).all()
    summary["loads"] = [
        {
            "load_id": load.id,
            "piece_id": load.piece_id,
            "weight_grams": load.weight_grams,
            "created_at": load.created_at.isoformat() if load.created_at else None,
        }
        for load in loads
    ]
    return summary


@app.post("/api/battens/{batten_id}/loads", status_code=201)
def create_load(batten_id: str, payload: LoadCreate, db: Session = Depends(get_db)):
    """逐片装载的原子裁决：行级锁串行化同一吊杆上的并发请求。"""
    if not MIN_WEIGHT_GRAMS <= payload.weight_grams <= MAX_WEIGHT_GRAMS:
        return reject(
            422,
            "INVALID_WEIGHT",
            f"单片重量必须在 {MIN_WEIGHT_GRAMS}～{MAX_WEIGHT_GRAMS} 克之间，"
            f"收到 {payload.weight_grams} 克",
        )

    # SELECT ... FOR UPDATE：锁定吊杆行，并发装载在此串行排队，
    # 后到的请求在锁释放后读到最新已提交总重再裁决。
    batten = db.scalar(
        select(Batten).where(Batten.id == batten_id).with_for_update()
    )
    if batten is None:
        db.rollback()
        return reject(404, "BATTEN_NOT_FOUND", f"吊杆 {batten_id} 不存在")

    existing = db.scalar(select(Load.id).where(Load.piece_id == payload.piece_id))
    if existing is not None:
        db.rollback()  # 失败请求不落库，同时释放行锁
        return reject(
            409,
            "PIECE_ID_EXISTS",
            f"配重片标识 {payload.piece_id} 已成功登记过，不能重复使用",
        )

    summary = batten_summary(db, batten)
    new_total = summary["total_grams"] + payload.weight_grams
    if new_total > batten.capacity_grams:
        db.rollback()
        return reject(
            409,
            "OVER_CAPACITY",
            f"吊杆 {batten_id} 当前总重 {summary['total_grams']} 克，"
            f"本片 {payload.weight_grams} 克，合计 {new_total} 克 "
            f"超出核定 {batten.capacity_grams} 克",
            **summary,
        )

    load = Load(
        batten_id=batten_id,
        piece_id=payload.piece_id,
        weight_grams=payload.weight_grams,
    )
    db.add(load)
    try:
        db.commit()
    except IntegrityError:
        # 并发下同一标识落在不同吊杆上时，由数据库唯一约束兜底
        db.rollback()
        return reject(
            409,
            "PIECE_ID_EXISTS",
            f"配重片标识 {payload.piece_id} 已成功登记过，不能重复使用",
        )

    return {
        "accepted": True,
        "message": f"配重片 {payload.piece_id} 已接纳",
        "batten_id": batten_id,
        "capacity_grams": batten.capacity_grams,
        "total_grams": new_total,
        "remaining_grams": batten.capacity_grams - new_total,
        "load": {
            "load_id": load.id,
            "piece_id": load.piece_id,
            "weight_grams": load.weight_grams,
        },
    }


def _lock_battens_in_fixed_order(db: Session, batten_ids: list[str]) -> dict[str, Batten]:
    """按吊杆编号固定顺序加行锁。

    转移涉及两根吊杆：无论方向是 G-01→G-02 还是 G-02→G-01，
    都按同一顺序（编号升序）逐行 SELECT ... FOR UPDATE，
    使相反方向的并发转移在锁队列上排成一队，避免相互等待形成死锁。
    """
    locked: dict[str, Batten] = {}
    for bid in sorted(set(batten_ids)):
        locked[bid] = db.scalar(
            select(Batten).where(Batten.id == bid).with_for_update()
        )
    return locked


@app.post("/api/battens/{batten_id}/loads/{load_id}/transfer")
def transfer_load(
    batten_id: str,
    load_id: int,
    payload: TransferCreate,
    db: Session = Depends(get_db),
):
    """把一片已登记配重片从源吊杆转移到目标吊杆。

    不新增/删除装载记录，只更新现有记录的归属，原始重量与登记时间保留。
    源、目标两根吊杆在同一事务内以固定顺序锁定，随后确认配重片仍属于
    源杆并校验目标余量，任一条件不满足则回滚，数据库保持原归属。
    """
    target_id = payload.target_batten_id

    locked = _lock_battens_in_fixed_order(db, [batten_id, target_id])
    source = locked.get(batten_id)
    target = locked.get(target_id)
    if source is None or target is None:
        missing = batten_id if source is None else target_id
        db.rollback()
        return reject(404, "BATTEN_NOT_FOUND", f"吊杆 {missing} 不存在")

    # 同杆判定放在存在性之后：源、目标填了同一个不存在的吊杆时，
    # 应明确报告吊杆不存在，而不是误报两杆相同
    if target_id == batten_id:
        db.rollback()  # 拒绝请求同时释放行锁
        return reject(
            422,
            "SAME_BATTEN",
            f"目标吊杆不能与源吊杆 {batten_id} 相同",
            source_batten_id=batten_id,
            target_batten_id=target_id,
        )

    # 锁内再确认配重片归属：并发转移时后到者会看到已提交的新归属
    load = db.scalar(
        select(Load).where(Load.id == load_id).with_for_update()
    )
    if load is None:
        # 编号从未登记过：明确报告装载记录不存在，
        # 不能与"已被其他终端移走"混为一谈
        db.rollback()
        return reject(
            404,
            "LOAD_NOT_FOUND",
            f"装载记录 {load_id} 不存在",
            source_batten_id=batten_id,
            target_batten_id=target_id,
            load_id=load_id,
        )
    if load.batten_id != batten_id:
        db.rollback()
        return reject(
            409,
            "POSITION_CHANGED",
            f"配重片当前位置已变化：已不在源吊杆 {batten_id} 上，"
            "可能已被其他终端转移",
            source_batten_id=batten_id,
            target_batten_id=target_id,
            load_id=load_id,
        )

    target_summary = batten_summary(db, target)
    new_target_total = target_summary["total_grams"] + load.weight_grams
    if new_target_total > target.capacity_grams:
        db.rollback()
        return reject(
            409,
            "OVER_CAPACITY",
            f"目标吊杆 {target_id} 当前总重 {target_summary['total_grams']} 克，"
            f"配重片 {load.piece_id} 重 {load.weight_grams} 克，"
            f"合计 {new_target_total} 克超出核定 {target.capacity_grams} 克",
            source_batten_id=batten_id,
            target_batten_id=target_id,
            **{
                f"target_{k}": v
                for k, v in target_summary.items()
                if k != "batten_id"
            },
        )

    # 只改归属：weight_grams 与 created_at 原样保留
    load.batten_id = target_id
    db.commit()

    source_summary = batten_summary(db, source)
    target_summary = batten_summary(db, target)
    return {
        "accepted": True,
        "message": f"配重片 {load.piece_id} 已从 {batten_id} 转移至 {target_id}",
        "load_id": load.id,
        "piece_id": load.piece_id,
        "weight_grams": load.weight_grams,
        "source_batten_id": batten_id,
        "target_batten_id": target_id,
        "source": source_summary,
        "target": target_summary,
    }


@app.post("/api/reset")
def reset(db: Session = Depends(get_db)):
    """恢复验收场景：清空全部装载记录，复位两根空吊杆。"""
    db.execute(delete(Load))
    ensure_fixture_battens(db)
    battens = db.scalars(select(Batten).order_by(Batten.id)).all()
    return {"battens": [batten_summary(db, batten) for batten in battens]}
