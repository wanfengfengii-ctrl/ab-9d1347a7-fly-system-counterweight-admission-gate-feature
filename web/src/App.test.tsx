import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import App from './App';

// 真实接口地址由 vite.config.ts 的 test.env 注入（容器内为 http://api:8000）
const BASE: string = import.meta.env.VITE_API_URL ?? '';

async function resetStage() {
  const res = await fetch(`${BASE}/api/reset`, { method: 'POST' });
  if (!res.ok) throw new Error(`reset 失败: ${res.status}`);
}

async function dbBatten(battenId: string) {
  const res = await fetch(`${BASE}/api/battens/${battenId}`);
  if (!res.ok) throw new Error(`查询吊杆失败: ${res.status}`);
  return res.json();
}

async function renderLoaded() {
  const user = userEvent.setup();
  render(<App />);
  // 等待真实接口返回、两根吊杆渲染完成
  await screen.findByRole('button', { name: /G-01/ });
  await screen.findByRole('button', { name: /G-02/ });
  return user;
}

async function submitPiece(
  user: ReturnType<typeof userEvent.setup>,
  pieceId: string,
  weight: string,
) {
  await user.clear(screen.getByLabelText('配重片标识'));
  await user.type(screen.getByLabelText('配重片标识'), pieceId);
  await user.clear(screen.getByLabelText('重量（克）'));
  await user.type(screen.getByLabelText('重量（克）'), weight);
  await user.click(screen.getByRole('button', { name: '登记装载' }));
}

beforeEach(resetStage);
afterEach(cleanup);

describe('吊杆配重装载页（真实接口反馈）', () => {
  it('页面加载后展示两根预置吊杆，且与数据库状态一致', async () => {
    await renderLoaded();

    const g01 = screen.getByRole('button', { name: /G-01/ });
    const g02 = screen.getByRole('button', { name: /G-02/ });
    expect(within(g01).getByText('核定 30000 克')).toBeInTheDocument();
    expect(within(g01).getByText('总重 0 克')).toBeInTheDocument();
    expect(within(g01).getByText('剩余 30000 克')).toBeInTheDocument();
    expect(within(g02).getByText('核定 50000 克')).toBeInTheDocument();

    // 默认选中 G-01，明细区展示空吊杆
    expect(await screen.findByTestId('total')).toHaveTextContent('0 克');
    expect(screen.getByTestId('remaining')).toHaveTextContent('30000 克');
    expect(screen.getByText('暂无已接纳配重片')).toBeInTheDocument();

    // 与数据库直接核对
    const db = await dbBatten('G-01');
    expect(db.total_grams).toBe(0);
    expect(db.remaining_grams).toBe(30000);
  });

  it('合法配重片被接纳，页面与数据库同步更新', async () => {
    const user = await renderLoaded();
    await submitPiece(user, 'CW-0001', '20000');

    const status = await screen.findByRole('status');
    expect(status).toHaveTextContent('已接纳');
    expect(status).toHaveTextContent('当前总重 20000 克');
    expect(status).toHaveTextContent('剩余量 10000 克');

    // 提交后页面会重新拉取接口，等待明细区刷新到最新状态
    await waitFor(() => expect(screen.getByTestId('total')).toHaveTextContent('20000 克'));
    expect(screen.getByTestId('remaining')).toHaveTextContent('10000 克');
    expect(screen.getByRole('cell', { name: 'CW-0001' })).toBeInTheDocument();

    const db = await dbBatten('G-01');
    expect(db.total_grams).toBe(20000);
    expect(db.remaining_grams).toBe(10000);
    expect(db.loads.map((l: { piece_id: string }) => l.piece_id)).toEqual(['CW-0001']);
  });

  it('恰好装满核定值时允许接纳', async () => {
    const user = await renderLoaded();
    await submitPiece(user, 'CW-FULL-1', '25000');
    await screen.findByRole('status');

    await submitPiece(user, 'CW-FULL-2', '5000');
    const status = await screen.findByRole('status');
    await waitFor(() => expect(status).toHaveTextContent('剩余量 0 克'));

    const db = await dbBatten('G-01');
    expect(db.total_grams).toBe(30000);
    expect(db.remaining_grams).toBe(0);
  });

  it('超出核定值被明确拒绝，且失败请求不落库', async () => {
    const user = await renderLoaded();
    await submitPiece(user, 'CW-KEEP', '20000');
    await screen.findByRole('status');

    await submitPiece(user, 'CW-OVER', '20000');
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('已拒绝');
    expect(alert).toHaveTextContent('超出核定');

    // 页面状态保持第一笔之后的样子
    await waitFor(() => expect(screen.getByTestId('total')).toHaveTextContent('20000 克'));
    expect(screen.getByTestId('remaining')).toHaveTextContent('10000 克');

    // 数据库里只有成功的那一笔
    const db = await dbBatten('G-01');
    expect(db.total_grams).toBe(20000);
    expect(db.loads).toHaveLength(1);
    expect(db.loads[0].piece_id).toBe('CW-KEEP');
  });

  it('同一标识在全库只能成功一次', async () => {
    const user = await renderLoaded();
    await submitPiece(user, 'CW-DUP', '5000');
    await screen.findByRole('status');

    // 换到 G-02 再用同一标识登记
    await user.selectOptions(screen.getByLabelText('选择吊杆'), 'G-02');
    await waitFor(() =>
      expect(screen.getByTestId('remaining')).toHaveTextContent('50000 克'),
    );

    await submitPiece(user, 'CW-DUP', '5000');
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('已拒绝');
    expect(alert).toHaveTextContent('不能重复使用');

    const db = await dbBatten('G-02');
    expect(db.total_grams).toBe(0);
    expect(db.loads).toHaveLength(0);
  });

  it('重量超出 100～25000 克范围被明确拒绝', async () => {
    const user = await renderLoaded();

    await submitPiece(user, 'CW-LIGHT', '50');
    expect(await screen.findByRole('alert')).toHaveTextContent('100～25000');

    await submitPiece(user, 'CW-HEAVY', '26000');
    // 第二次拒绝会替换提示内容，等待新文案出现
    await waitFor(() =>
      expect(screen.getByRole('alert')).toHaveTextContent('收到 26000 克'),
    );
    expect(screen.getByRole('alert')).toHaveTextContent('100～25000');

    const db = await dbBatten('G-01');
    expect(db.total_grams).toBe(0);
    expect(db.loads).toHaveLength(0);
  });

  it('小数或非整数输入在页面被明确拒绝且不保存', async () => {
    const user = await renderLoaded();

    await submitPiece(user, 'CW-DEC', '100.0');
    expect(await screen.findByRole('alert')).toHaveTextContent('整数克数');

    await submitPiece(user, 'CW-ABC', 'abc');
    expect(await screen.findByRole('alert')).toHaveTextContent('整数克数');

    const db = await dbBatten('G-01');
    expect(db.total_grams).toBe(0);
    expect(db.loads).toHaveLength(0);
  });

  it('接口严格拒绝字符串或小数重量（真实接口）', async () => {
    // 手工构造原始 JSON 报文：JS 的 JSON.stringify(100.0) 会变成整数 100
    const badBodies = [
      '{"piece_id":"CW-STRICT-S1","weight_grams":"100"}',
      '{"piece_id":"CW-STRICT-S2","weight_grams":100.0}',
      '{"piece_id":"CW-STRICT-S3","weight_grams":"20000"}',
    ];
    for (const raw of badBodies) {
      const res = await fetch(`${BASE}/api/battens/G-01/loads`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: raw,
      });
      expect(res.status).toBe(422);
      const body = await res.json();
      expect(body.accepted).toBe(false);
      expect(body.reason).toBe('INVALID_INPUT');
    }

    const db = await dbBatten('G-01');
    expect(db.total_grams).toBe(0);
    expect(db.loads).toHaveLength(0);
  });

  it('页面重新渲染（刷新）后状态仍与数据库一致', async () => {
    const user = await renderLoaded();
    await submitPiece(user, 'CW-PERSIST', '12000');
    await screen.findByRole('status');
    cleanup();

    // 模拟刷新：全新渲染，数据只能来自接口
    render(<App />);
    expect(await screen.findByTestId('total')).toHaveTextContent('12000 克');
    expect(screen.getByTestId('remaining')).toHaveTextContent('18000 克');
    expect(screen.getByRole('cell', { name: 'CW-PERSIST' })).toBeInTheDocument();

    const db = await dbBatten('G-01');
    expect(db.total_grams).toBe(12000);
    expect(db.remaining_grams).toBe(18000);
  });

  it('页面转移配重片：选择目标吊杆确认后，源杆与目标杆均刷新', async () => {
    const user = await renderLoaded();
    await submitPiece(user, 'CW-MOVE', '20000');
    await screen.findByRole('status');

    // 先从数据库记录原始登记信息，用于核对转移后重量与登记时间保留
    const before = await dbBatten('G-01');
    const movedBefore = before.loads.find(
      (l: { piece_id: string }) => l.piece_id === 'CW-MOVE',
    );
    expect(movedBefore).toBeTruthy();

    // 当前吊杆明细中为每片显示“转移”
    const transferBtn = screen.getByRole('button', { name: '转移 CW-MOVE' });
    await user.click(transferBtn);

    // 确认面板出现，目标吊杆默认是另一根 G-02
    const panel = screen.getByRole('group', { name: '转移确认' });
    expect(panel).toBeInTheDocument();
    const targetSelect = screen.getByLabelText('目标吊杆');
    expect((targetSelect as HTMLSelectElement).value).toBe('G-02');

    await user.click(screen.getByRole('button', { name: '确认转移' }));

    // 成功提示给出配重片去向与两杆刷新后的总重 / 余量
    const status = await screen.findByRole('status');
    expect(status).toHaveTextContent('已从 G-01 转移至 G-02');
    expect(status).toHaveTextContent('G-01 总重 0 克');
    expect(status).toHaveTextContent('剩余 30000 克');
    expect(status).toHaveTextContent('G-02 总重 20000 克');
    expect(status).toHaveTextContent('剩余 30000 克');

    // 刷新后的源杆：总重 / 余量归零状态，明细中不再有该片。
    // 用整串正则避免 "0 克" 误匹配到转移前的 "20000 克"（子串包含）。
    await waitFor(() =>
      expect(screen.getByTestId('total')).toHaveTextContent(/^0 克$/),
    );
    expect(screen.getByTestId('remaining')).toHaveTextContent(/^30000 克$/);
    expect(screen.getByText('暂无已接纳配重片')).toBeInTheDocument();
    expect(
      screen.queryByRole('cell', { name: 'CW-MOVE' }),
    ).not.toBeInTheDocument();

    // 直接与数据库核对：源杆已空，目标杆挂有同一笔记录（同一 load_id），
    // 原始重量与登记时间保留
    const src = await dbBatten('G-01');
    expect(src.total_grams).toBe(0);
    expect(src.remaining_grams).toBe(30000);
    expect(src.loads).toHaveLength(0);

    const dst = await dbBatten('G-02');
    expect(dst.total_grams).toBe(20000);
    expect(dst.remaining_grams).toBe(30000);
    expect(dst.loads).toHaveLength(1);
    const movedAfter = dst.loads[0];
    expect(movedAfter.load_id).toBe(movedBefore.load_id);
    expect(movedAfter.piece_id).toBe('CW-MOVE');
    expect(movedAfter.weight_grams).toBe(20000);
    expect(movedAfter.created_at).toBe(movedBefore.created_at);

    // 页面切到目标杆后，明细同样来自接口并显示该片
    await user.selectOptions(screen.getByLabelText('选择吊杆'), 'G-02');
    await waitFor(() =>
      expect(screen.getByRole('cell', { name: 'CW-MOVE' })).toBeInTheDocument(),
    );
    expect(screen.getByTestId('total')).toHaveTextContent('20000 克');
    expect(screen.getByTestId('remaining')).toHaveTextContent('30000 克');
  });

  it('目标吊杆余量不足时转移被拒绝，页面与数据库均保持原归属', async () => {
    // 直接经接口布阵：G-01 挂 20000 克，G-02 已占 35000 克（余 15000 克）
    await fetch(`${BASE}/api/battens/G-01/loads`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ piece_id: 'CW-STAY', weight_grams: 20000 }),
    });
    for (const [id, w] of [
      ['CW-OCC-A', 20000],
      ['CW-OCC-B', 15000],
    ] as const) {
      await fetch(`${BASE}/api/battens/G-02/loads`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ piece_id: id, weight_grams: w }),
      });
    }

    const user = await renderLoaded();
    await waitFor(() =>
      expect(screen.getByRole('cell', { name: 'CW-STAY' })).toBeInTheDocument(),
    );

    await user.click(screen.getByRole('button', { name: '转移 CW-STAY' }));
    await user.click(screen.getByRole('button', { name: '确认转移' }));

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('已拒绝');
    expect(alert).toHaveTextContent('超出核定');

    // 源杆明细不变
    expect(screen.getByRole('cell', { name: 'CW-STAY' })).toBeInTheDocument();
    expect(screen.getByTestId('total')).toHaveTextContent('20000 克');
    expect(screen.getByTestId('remaining')).toHaveTextContent('10000 克');

    // 数据库保持原归属
    const src = await dbBatten('G-01');
    expect(src.loads.map((l: { piece_id: string }) => l.piece_id)).toEqual([
      'CW-STAY',
    ]);
    const dst = await dbBatten('G-02');
    expect(dst.total_grams).toBe(35000);
    expect(dst.remaining_grams).toBe(15000);
  });

  it('修正重量成功：刷新总重、余量与明细，配重标识与最初登记时间保留', async () => {
    const user = await renderLoaded();
    await submitPiece(user, 'CW-FIX', '20000');

    // 提交成功后页面会重新拉取接口，等待明细刷新出该片的“修正重量”入口
    const fixButton = await screen.findByRole('button', {
      name: '修正 CW-FIX 重量',
    });

    // 修正前先从数据库记录原始登记信息，用于核对修正后是同一条记录
    const before = await dbBatten('G-01');
    const beforeLoad = before.loads.find(
      (l: { piece_id: string }) => l.piece_id === 'CW-FIX',
    );
    expect(beforeLoad).toBeTruthy();

    // 点击后出现确认面板并预填当前重量
    await user.click(fixButton);
    const panel = screen.getByRole('group', { name: '修正重量确认' });
    expect(panel).toBeInTheDocument();
    const weightInput = screen.getByLabelText('新重量（克）');
    expect((weightInput as HTMLInputElement).value).toBe('20000');

    await user.clear(weightInput);
    await user.type(weightInput, '12000');
    await user.click(screen.getByRole('button', { name: '确认修正' }));

    // 成功提示明确展示由多少修正为多少，以及刷新后的总重 / 余量
    const status = await screen.findByRole('status');
    expect(status).toHaveTextContent('已由 20000 克修正为 12000 克');
    expect(status).toHaveTextContent('当前总重 12000 克');
    expect(status).toHaveTextContent('剩余量 18000 克');

    // 提交后页面重新拉取接口：该杆总重、余量与明细全部刷新
    await waitFor(() =>
      expect(screen.getByTestId('total')).toHaveTextContent('12000 克'),
    );
    expect(screen.getByTestId('remaining')).toHaveTextContent('18000 克');
    expect(screen.getByRole('cell', { name: '12000' })).toBeInTheDocument();
    expect(
      screen.queryByRole('cell', { name: '20000' }),
    ).not.toBeInTheDocument();

    // 与数据库核对：仍是同一条记录，只改了重量，标识与登记时间保留
    const db = await dbBatten('G-01');
    expect(db.total_grams).toBe(12000);
    expect(db.remaining_grams).toBe(18000);
    expect(db.loads).toHaveLength(1);
    const afterLoad = db.loads[0];
    expect(afterLoad.load_id).toBe(beforeLoad.load_id);
    expect(afterLoad.piece_id).toBe('CW-FIX');
    expect(afterLoad.weight_grams).toBe(12000);
    expect(afterLoad.created_at).toBe(beforeLoad.created_at);
  });

  it('增重至恰好满载允许修正', async () => {
    const user = await renderLoaded();
    await submitPiece(user, 'CW-UP-A', '10000');
    await screen.findByRole('status');
    await submitPiece(user, 'CW-UP-B', '10000');
    await screen.findByRole('status');

    // 10000 + 20000 = 30000，恰好达到核定值
    await user.click(
      await screen.findByRole('button', { name: '修正 CW-UP-B 重量' }),
    );
    const input = screen.getByLabelText('新重量（克）');
    await user.clear(input);
    await user.type(input, '20000');
    await user.click(screen.getByRole('button', { name: '确认修正' }));

    const status = await screen.findByRole('status');
    expect(status).toHaveTextContent('当前总重 30000 克');
    expect(status).toHaveTextContent('剩余量 0 克');

    await waitFor(() =>
      expect(screen.getByTestId('remaining')).toHaveTextContent(/^0 克$/),
    );
    expect(screen.getByTestId('total')).toHaveTextContent('30000 克');

    const db = await dbBatten('G-01');
    expect(db.total_grams).toBe(30000);
    expect(db.remaining_grams).toBe(0);
  });

  it('修正后超载被明确拒绝，数据库中的原重量保持不变', async () => {
    const user = await renderLoaded();
    await submitPiece(user, 'CW-OV-A', '10000');
    await screen.findByRole('status');
    await submitPiece(user, 'CW-OV-B', '10000');
    await screen.findByRole('status');

    // 10000 + 20001 = 30001 > 30000
    await user.click(
      await screen.findByRole('button', { name: '修正 CW-OV-B 重量' }),
    );
    const input = screen.getByLabelText('新重量（克）');
    await user.clear(input);
    await user.type(input, '20001');
    await user.click(screen.getByRole('button', { name: '确认修正' }));

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('已拒绝');
    expect(alert).toHaveTextContent('超出核定');

    // 刷新后仍是修正前的总重 / 余量
    await waitFor(() =>
      expect(screen.getByTestId('total')).toHaveTextContent('20000 克'),
    );
    expect(screen.getByTestId('remaining')).toHaveTextContent('10000 克');

    // 数据库原重量保持不变
    const db = await dbBatten('G-01');
    expect(db.total_grams).toBe(20000);
    expect(db.remaining_grams).toBe(10000);
    const kept = db.loads.find(
      (l: { piece_id: string }) => l.piece_id === 'CW-OV-B',
    );
    expect(kept.weight_grams).toBe(10000);
  });

  it('新重量超出单片范围被明确拒绝且不修改', async () => {
    const user = await renderLoaded();
    await submitPiece(user, 'CW-BADW', '1000');
    await screen.findByRole('status');

    await user.click(
      await screen.findByRole('button', { name: '修正 CW-BADW 重量' }),
    );
    const input = screen.getByLabelText('新重量（克）');
    await user.clear(input);
    await user.type(input, '50');
    await user.click(screen.getByRole('button', { name: '确认修正' }));

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('已拒绝');
    expect(alert).toHaveTextContent('100～25000');

    const db = await dbBatten('G-01');
    expect(db.total_grams).toBe(1000);
    expect(db.loads[0].weight_grams).toBe(1000);
  });
});
