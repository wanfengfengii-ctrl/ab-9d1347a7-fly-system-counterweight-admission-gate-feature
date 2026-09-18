import { FormEvent, useCallback, useEffect, useRef, useState } from 'react';
import { fetchBatten, fetchBattens, submitLoad, transferLoad } from './api';
import type { BattenDetail, BattenSummary, LoadItem } from './types';

interface Feedback {
  kind: 'success' | 'error';
  text: string;
}

export default function App() {
  const [battens, setBattens] = useState<BattenSummary[]>([]);
  const [selected, setSelected] = useState('G-01');
  const [detail, setDetail] = useState<BattenDetail | null>(null);
  const [pieceId, setPieceId] = useState('');
  const [weight, setWeight] = useState('');
  const [feedback, setFeedback] = useState<Feedback | null>(null);
  const [submitting, setSubmitting] = useState(false);
  // 待转移的配重片（来自当前吊杆明细）；null 表示未进入转移流程
  const [transferring, setTransferring] = useState<LoadItem | null>(null);
  const [transferTarget, setTransferTarget] = useState('');

  // 单调递增的刷新序号：放弃早于最新一次刷新返回的过期响应，
  // 避免上一个动作的在途拉取在新动作之后落地，把界面回滚成旧状态
  const refreshSeq = useRef(0);

  // 页面状态永远以数据库为准：挂载与每次登记/转移后都重新拉取
  const refresh = useCallback(async (battenId: string) => {
    const seq = ++refreshSeq.current;
    const [list, det] = await Promise.all([fetchBattens(), fetchBatten(battenId)]);
    // 丢弃在更早动作中发出、却晚于最新刷新落地的过期响应
    if (seq !== refreshSeq.current) return;
    if (list.status === 200) {
      setBattens(list.body.battens);
    }
    if (det.status === 200) {
      setDetail(det.body);
    } else {
      setDetail(null);
    }
  }, []);

  useEffect(() => {
    void refresh(selected);
  }, [refresh, selected]);

  // 切换吊杆后，上一根吊杆的待转移配重片不再适用于当前明细
  useEffect(() => {
    setTransferring(null);
    setTransferTarget('');
  }, [selected]);

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    setFeedback(null);
    const trimmedId = pieceId.trim();
    const trimmedWeight = weight.trim();
    // 按原始输入严格判定整数：字符串 "100.0"、"1e2" 等一律在页面明确拒绝
    if (!trimmedId || !/^-?\d+$/.test(trimmedWeight)) {
      setFeedback({ kind: 'error', text: '请输入配重片标识，重量必须是整数克数' });
      return;
    }
    const grams = Number(trimmedWeight);
    setSubmitting(true);
    try {
      const { body } = await submitLoad(selected, trimmedId, grams);
      if (body.accepted) {
        setFeedback({
          kind: 'success',
          text: `${body.message}：当前总重 ${body.total_grams} 克，剩余量 ${body.remaining_grams} 克`,
        });
        setPieceId('');
        setWeight('');
      } else {
        setFeedback({ kind: 'error', text: `已拒绝：${body.message}` });
      }
    } catch {
      setFeedback({ kind: 'error', text: '网络错误，无法联系装载裁决服务' });
    } finally {
      setSubmitting(false);
    }
    await refresh(selected);
  }

  function beginTransfer(load: LoadItem) {
    setFeedback(null);
    setTransferring(load);
    // 默认目标为另一根吊杆
    setTransferTarget(battens.find((b) => b.batten_id !== selected)?.batten_id ?? '');
  }

  function cancelTransfer() {
    setTransferring(null);
    setTransferTarget('');
  }

  async function confirmTransfer() {
    if (!transferring || !transferTarget) return;
    setFeedback(null);
    setSubmitting(true);
    try {
      const { body } = await transferLoad(
        selected,
        transferring.load_id,
        transferTarget,
      );
      if (body.accepted) {
        setFeedback({
          kind: 'success',
          text: `${body.message}：${body.source_batten_id} 总重 ${body.source?.total_grams ?? ''} 克、剩余 ${body.source?.remaining_grams ?? ''} 克；${body.target_batten_id} 总重 ${body.target?.total_grams ?? ''} 克、剩余 ${body.target?.remaining_grams ?? ''} 克`,
        });
        setTransferring(null);
        setTransferTarget('');
      } else {
        // 当前位置已变化等拒绝后，明细需要以数据库为准刷新
        setFeedback({ kind: 'error', text: `已拒绝：${body.message}` });
        setTransferring(null);
        setTransferTarget('');
      }
    } catch {
      setFeedback({ kind: 'error', text: '网络错误，无法联系装载裁决服务' });
    } finally {
      setSubmitting(false);
    }
    // 无论成功或拒绝，都刷新两根吊杆总重、余量与明细
    await refresh(selected);
  }

  return (
    <main className="page">
      <h1>剧场吊杆配重装载台</h1>

      <section aria-label="吊杆总览" className="cards">
        {battens.map((b) => (
          <button
            key={b.batten_id}
            type="button"
            className={b.batten_id === selected ? 'card selected' : 'card'}
            aria-pressed={b.batten_id === selected}
            onClick={() => setSelected(b.batten_id)}
          >
            <strong>{b.batten_id}</strong>
            <span>核定 {b.capacity_grams} 克</span>
            <span>总重 {b.total_grams} 克</span>
            <span>剩余 {b.remaining_grams} 克</span>
          </button>
        ))}
      </section>

      <form className="load-form" noValidate onSubmit={handleSubmit}>
        <label htmlFor="batten-select">选择吊杆</label>
        <select
          id="batten-select"
          value={selected}
          onChange={(e) => setSelected(e.target.value)}
        >
          {battens.map((b) => (
            <option key={b.batten_id} value={b.batten_id}>
              {b.batten_id}
            </option>
          ))}
        </select>

        <label htmlFor="piece-id">配重片标识</label>
        <input
          id="piece-id"
          value={pieceId}
          placeholder="例如 CW-0001"
          onChange={(e) => setPieceId(e.target.value)}
        />

        <label htmlFor="weight">重量（克）</label>
        <input
          id="weight"
          type="text"
          inputMode="numeric"
          value={weight}
          placeholder="100 ~ 25000 的整数"
          onChange={(e) => setWeight(e.target.value)}
        />

        <button type="submit" disabled={submitting}>
          登记装载
        </button>
      </form>

      {feedback && (
        <p
          role={feedback.kind === 'error' ? 'alert' : 'status'}
          className={`feedback ${feedback.kind}`}
        >
          {feedback.text}
        </p>
      )}

      {detail && (
        <section aria-label="吊杆明细" className="detail">
          <h2>{detail.batten_id} 当前状态</h2>
          <dl className="stats">
            <div>
              <dt>核定重量</dt>
              <dd>{detail.capacity_grams} 克</dd>
            </div>
            <div>
              <dt>当前总重</dt>
              <dd data-testid="total">{detail.total_grams} 克</dd>
            </div>
            <div>
              <dt>剩余量</dt>
              <dd data-testid="remaining">{detail.remaining_grams} 克</dd>
            </div>
          </dl>
          {detail.loads.length === 0 ? (
            <p>暂无已接纳配重片</p>
          ) : (
            <table>
              <thead>
                <tr>
                  <th>配重片标识</th>
                  <th>重量（克）</th>
                  <th>登记时间</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {detail.loads.map((load) => (
                  <tr key={load.load_id}>
                    <td>{load.piece_id}</td>
                    <td>{load.weight_grams}</td>
                    <td>
                      {load.created_at
                        ? new Date(load.created_at).toLocaleString('zh-CN', {
                            hour12: false,
                          })
                        : ''}
                    </td>
                    <td>
                      <button
                        type="button"
                        className="transfer-btn"
                        aria-label={`转移 ${load.piece_id}`}
                        onClick={() => beginTransfer(load)}
                        disabled={submitting}
                      >
                        转移
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          {transferring && (
            <div className="transfer-panel" role="group" aria-label="转移确认">
              <p>
                将配重片 <strong>{transferring.piece_id}</strong>（
                {transferring.weight_grams} 克）从 {selected} 转移到：
              </p>
              <label htmlFor="transfer-target">目标吊杆</label>
              <select
                id="transfer-target"
                value={transferTarget}
                onChange={(e) => setTransferTarget(e.target.value)}
              >
                {battens
                  .filter((b) => b.batten_id !== selected)
                  .map((b) => (
                    <option key={b.batten_id} value={b.batten_id}>
                      {b.batten_id}（剩余 {b.remaining_grams} 克）
                    </option>
                  ))}
              </select>
              <button
                type="button"
                className="confirm-transfer"
                disabled={submitting || !transferTarget}
                onClick={() => void confirmTransfer()}
              >
                确认转移
              </button>
              <button
                type="button"
                className="cancel-transfer"
                disabled={submitting}
                onClick={cancelTransfer}
              >
                取消
              </button>
            </div>
          )}
        </section>
      )}
    </main>
  );
}
