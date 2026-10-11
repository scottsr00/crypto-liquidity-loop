// Update account values and freshness without disturbing owner control forms.
const lookup = (state, path) => path.split('.').reduce((value, key) => value?.[key], state);
const money = value => value == null ? '—' : Number(value).toLocaleString('en-US', {style: 'currency', currency: 'USD'});
const percent = value => value == null ? '—' : Number(value).toLocaleString('en-US', {style: 'percent', minimumFractionDigits: 2});
async function refresh() {
  const badge = document.querySelector('.header-status .badge');
  try {
    const response = await fetch('/api/state', {cache: 'no-store'});
    if (!response.ok) throw new Error(response.status === 401 ? 'Owner session expired' : 'Dashboard unavailable');
    const state = await response.json();
    const now = Date.now() / 1000;
    const alive = now - (state.heartbeat || 0) < 15;
    const fresh = state.quote && now - state.quote.timestamp <= state.settings.quote_max_age_seconds;
    badge.textContent = !alive ? 'worker offline' : !fresh ? 'stale data' : state.health.replaceAll('_', ' ');
    badge.className = `badge ${alive && fresh ? 'good' : 'warning'}`;
    document.querySelector('.header-status p').textContent = state.quote ? `Quote ${new Date(state.quote.timestamp * 1000).toISOString()}` : 'No fresh quote';
    document.querySelectorAll('[data-money]').forEach(element => element.textContent = money(lookup(state, element.dataset.money)));
    document.querySelectorAll('[data-percent]').forEach(element => element.textContent = percent(lookup(state, element.dataset.percent)));
    document.querySelectorAll('[data-quantity]').forEach(element => element.textContent = `${lookup(state, element.dataset.quantity)} BTC`);
    const status = document.querySelector('#run-status');
    status.textContent = state.halted ? `Trading halted. ${state.liquidating ? `Liquidation pending; ${state.btc} BTC remains.` : 'Owner resume required.'}` : state.paused ? 'Entries paused. Exits and risk monitoring continue.' : 'Paper strategy active';
  } catch (error) {
    badge.textContent = error.message;
    badge.className = 'badge warning';
  }
}
setInterval(refresh, 5000);
