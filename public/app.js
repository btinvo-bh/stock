const $ = (selector, scope = document) => scope.querySelector(selector);
const $$ = (selector, scope = document) => [...scope.querySelectorAll(selector)];
const today = () => {
  const date = new Date();
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
};
const money = value => new Intl.NumberFormat(undefined, { style: 'currency', currency: state.data?.currency || 'INR' }).format(Number(value || 0));
const number = value => new Intl.NumberFormat(undefined, { maximumFractionDigits: 3 }).format(Number(value || 0));
const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[char]);
const state = { view: 'overview', user: null, data: null, category: 'ALL', currentStockCategory: 'ALL', itemQuery: '', report: null, reportFrom: '', reportTo: '', reportCategory: 'ALL', reportMovementType: 'ALL' };

function monthStart() {
  const date = new Date();
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-01`;
}

async function api(path, options = {}) {
  const isFormData = typeof FormData !== 'undefined' && options.body instanceof FormData;
  const response = await fetch(path, {
    credentials: 'same-origin',
    ...options,
    headers: { ...(options.body && !isFormData ? { 'Content-Type': 'application/json' } : {}), ...options.headers },
  });
  const result = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 401 && path !== '/api/login') showLogin();
    throw new Error(result.error || 'Something went wrong. Please try again.');
  }
  return result;
}

function notify(message, isError = false) {
  const toast = $('#toast');
  toast.textContent = message;
  toast.classList.toggle('error', isError);
  toast.classList.add('visible');
  clearTimeout(notify.timer);
  notify.timer = setTimeout(() => toast.classList.remove('visible'), 3000);
}

function showLogin() {
  $('#app-shell').hidden = true;
  $('#login-screen').hidden = false;
}

function showApp() {
  $('#login-screen').hidden = true;
  $('#app-shell').hidden = false;
  $('#sidebar-username').textContent = state.user.username;
  $('#sidebar-avatar').textContent = state.user.username.slice(0, 1).toUpperCase();
  $('#sidebar-role').textContent = state.user.role;
  $('#today-label').textContent = new Intl.DateTimeFormat(undefined, { weekday: 'short', month: 'short', day: 'numeric' }).format(new Date());
  $('#users-nav').hidden = !['master', 'admin'].includes(state.user.role);
  $('#settings-nav').hidden = !['master', 'admin'].includes(state.user.role);
  render();
}

async function loadData() {
  state.data = await api('/api/bootstrap');
  state.user = state.data.user;
  showApp();
}

function roleLabel(role) {
  return ({ master: 'Master user', admin: 'Administrator', staff: 'Staff' })[role] || role;
}

function activeItems() {
  return (state.data?.items || []).filter(item => item.active);
}

function categoryOptions(selected = 'ALL') {
  return `<option value="ALL" ${selected === 'ALL' ? 'selected' : ''}>All categories</option>${(state.data?.categories || []).map(category => `<option value="${esc(category)}" ${selected === category ? 'selected' : ''}>${esc(category)}</option>`).join('')}`;
}

function supplierOptions(selected = '') {
  return `<option value="">No supplier</option>${(state.data?.suppliers || []).filter(supplier => supplier.active).map(supplier => `<option value="${supplier.id}" ${String(selected) === String(supplier.id) ? 'selected' : ''}>${esc(supplier.name)}</option>`).join('')}`;
}

function itemOptions() {
  return activeItems().map(item => `<option value="${esc(itemChoice(item))}"></option>`).join('');
}

function itemChoice(item) {
  return `${item.name} (${item.supplier_item_code || 'no code'}) · ${item.id}`;
}

function orderOptions(itemId = null) {
  return `<option value="">No linked order</option>${(state.data?.orders || []).filter(order => ['open', 'partial'].includes(order.status) && (!itemId || Number(order.item_id) === Number(itemId))).map(order => `<option value="${order.id}">#${order.id} · ${esc(order.item_name)} · ${number(order.remaining_quantity)} remaining</option>`).join('')}`;
}

function itemSearchField() {
  return `<label>Item name <input name="item_choice" list="item-options" placeholder="Search item name or code" autocomplete="off" required><datalist id="item-options">${itemOptions()}</datalist></label>`;
}

function getItemFromChoice(value) {
  const id = Number(String(value).match(/·\s*(\d+)$/)?.[1]);
  return activeItems().find(item => item.id === id);
}

function packLabel(item) {
  const uom = (state.data?.uoms || []).find(unit => unit.name === item.uom);
  return `${number(item.pack_count || 1)} × ${number(item.units_per_pack || item.pack_size)} = ${number(item.pack_size)} ${item.base_unit || uom?.base_unit || uom?.pack_unit || item.uom}`;
}

function balanceLabel(item) {
  return `${number(item.on_hand)} ${item.uom} · ${number(item.on_hand_base)} ${item.base_unit || item.uom}`;
}

function packageQuantityLabel(quantity, baseQuantity, baseUnit) {
  return `${number(quantity)} packages (${number(baseQuantity)} ${esc(baseUnit)})`;
}

function heading(kicker, title, description, actions = '', extraClass = '') {
  return `<div class="page-heading ${extraClass}"><div><span class="page-kicker">${esc(kicker)}</span><h1>${esc(title)}</h1><p>${esc(description)}</p></div>${actions ? `<div class="heading-actions">${actions}</div>` : ''}</div>`;
}

function reportPdfUrl() {
  const params = new URLSearchParams({
    from: state.reportFrom || monthStart(),
    to: state.reportTo || today(),
    category: state.reportCategory || 'ALL',
    movement_type: state.reportMovementType || 'ALL',
  });
  return `/api/reports.pdf?${params}`;
}

function reportDescription() {
  const movement = state.reportMovementType === 'stock_in'
    ? 'Stock in only'
    : state.reportMovementType === 'stock_out' ? 'Stock out only' : 'Stock in and out';
  return `Inventory from ${state.reportFrom || monthStart()} to ${state.reportTo || today()} · ${state.reportCategory === 'ALL' ? 'All categories' : state.reportCategory} · ${movement}`;
}

function table(headers, rows, empty = 'Nothing to show yet.') {
  if (!rows.length) return `<div class="empty-state">${esc(empty)}</div>`;
  return `<div class="table-wrap"><table><thead><tr>${headers.map(header => `<th>${header}</th>`).join('')}</tr></thead><tbody>${rows.join('')}</tbody></table></div>`;
}

function itemCell(item) {
  return `<span class="table-item"><strong>${esc(item.name ?? item.item_name)}</strong><small>${esc(item.supplier_item_code || 'No supplier code')}</small></span>`;
}

function render() {
  if (!state.data) return;
  if (state.view === 'users' && !['master', 'admin'].includes(state.user.role)) state.view = 'overview';
  const labels = { overview: 'Overview', items: 'Items', 'current-stock': 'Current stock', suppliers: 'Suppliers', receiving: 'Stock in', issues: 'Stock out', orders: 'Purchase orders', reports: 'Reports', users: 'Users', settings: 'Settings' };
  $('#crumb-label').textContent = labels[state.view] || 'Overview';
  $$('.nav-item[data-view]').forEach(button => button.classList.toggle('active', button.dataset.view === state.view));
  const page = $('#page-content');
  page.innerHTML = ({
    overview: renderOverview,
    items: renderItems,
    'current-stock': renderCurrentStock,
    suppliers: renderSuppliers,
    receiving: renderReceiving,
    issues: renderIssues,
    orders: renderOrders,
    reports: renderReports,
    users: renderUsers,
    settings: renderSettings,
  }[state.view] || renderOverview)();
  if (state.view === 'reports') loadReport();
  if (state.view === 'settings' && state.user.role === 'master') loadDriveStatus();
}

function renderOverview() {
  const category = state.category;
  const items = activeItems().filter(item => category === 'ALL' || item.category === category);
  const inStock = items.filter(item => Number(item.on_hand_base) > 0).length;
  const inventoryValue = items.reduce((sum, item) => sum + Math.max(Number(item.on_hand), 0) * Number(item.last_price || 0), 0);
  const lowStock = items.filter(item => Number(item.on_hand_base) <= Number(item.reorder_level));
  const orderCount = state.data.orders.filter(order => ['open', 'partial'].includes(order.status)).length;
  const categories = state.data.categories.map(name => ({ name, count: activeItems().filter(item => item.category === name).length }));
  const maxCount = Math.max(1, ...categories.map(row => row.count));
  const categoryRows = categories.map(row => `<div class="category-row"><span class="category-name">${esc(row.name)}</span><span class="category-track"><span class="category-fill" style="width:${Math.max(row.count ? 8 : 0, row.count / maxCount * 100)}%"></span></span><span class="category-count">${row.count}</span></div>`).join('');
  const alerts = lowStock.slice(0, 6).map(item => `<div class="alert-row"><span><b>${esc(item.name)}</b><br><small>${esc(item.category)} · ${esc(item.supplier_name || 'No supplier')}</small></span><span class="stock-badge">${esc(balanceLabel(item))}</span></div>`).join('');
  const movementRows = state.data.movements.slice(0, 6).map(movement => `<tr><td>${esc(movement.movement_date)}</td><td><strong>${esc(movement.item_name)}</strong></td><td><span class="tag ${movement.movement_type === 'stock_out' ? 'inactive' : ''}">${movement.movement_type === 'stock_in' ? 'Stock in' : 'Stock out'}</span></td><td>${packageQuantityLabel(movement.quantity, movement.base_quantity, movement.base_unit)}</td><td>${esc(movement.username)}</td></tr>`);
  return `${heading('INVENTORY AT A GLANCE', 'Good morning', 'A practical snapshot of your stock and recent activity.', `<select class="filter-select" id="overview-category" aria-label="Filter category">${categoryOptions(category)}</select>`)}
    <section class="overview-grid">
      <article class="stat-card"><div class="stat-top"><span>Products in stock</span><span class="stat-dot green"></span></div><div class="stat-value">${number(inStock)}</div><div class="stat-note">Of ${items.length} active products</div></article>
      <article class="stat-card"><div class="stat-top"><span>Stock value</span><span class="stat-dot lime"></span></div><div class="stat-value">${money(inventoryValue)}</div><div class="stat-note">At most recent purchase prices</div></article>
      <article class="stat-card"><div class="stat-top"><span>Low stock</span><span class="stat-dot red"></span></div><div class="stat-value">${lowStock.length}</div><div class="stat-note">At or below each item’s reorder level</div></article>
      <article class="stat-card"><div class="stat-top"><span>Open orders</span><span class="stat-dot"></span></div><div class="stat-value">${orderCount}</div><div class="stat-note">Open or partially received</div></article>
    </section>
    <section class="section-grid"><div class="section-panel"><div class="panel-heading"><h2>Items by category</h2><small>Active catalogue</small></div><div class="category-list">${categoryRows}</div></div>
      <div class="section-panel"><div class="panel-heading"><h2>Low stock alert</h2><button class="text-link" data-view="reports">View report</button></div><div class="alert-list">${alerts || '<div class="empty-state">All active items are above the alert level.</div>'}</div></div></section>
    <section class="section-panel" style="margin-top:16px"><div class="panel-heading"><h2>Recent stock movements</h2><button class="text-link" data-view="receiving">View stock in</button></div>${table(['Date', 'Item', 'Movement', 'Quantity', 'Recorded by'], movementRows, 'Stock movements will appear here.')}</section>`;
}

function renderCurrentStock() {
  const category = state.currentStockCategory;
  const items = activeItems().filter(item => category === 'ALL' || item.category === category);
  const canManageItems = ['master', 'admin'].includes(state.user.role);
  const rows = items.map(item => `<tr><td>${itemCell(item)}</td><td>${esc(item.category)}</td><td>${esc(item.supplier_name || '—')}</td><td>${esc(packLabel(item))}</td><td class="${Number(item.on_hand_base) <= Number(item.reorder_level) ? 'quantity-low' : ''}">${esc(balanceLabel(item))}</td><td>${money(item.last_price ?? item.default_price)}</td><td>${money(Math.max(Number(item.on_hand), 0) * Number(item.last_price ?? item.default_price ?? 0))}</td>${canManageItems ? `<td><div class="table-actions"><button class="button button-secondary button-small" data-edit-item="${item.id}">Edit</button><button class="button button-danger button-small" data-archive-item="${item.id}">Archive</button></div></td>` : ''}</tr>`);
  return `${heading('LIVE INVENTORY', 'Current stock', 'On-hand balances from all recorded receipts and issues.', `<select class="filter-select" id="current-stock-category" aria-label="Filter current stock by category">${categoryOptions(category)}</select>`)}
    <section class="section-panel table-panel">${table(['Item / code', 'Category', 'Supplier', 'Package conversion', 'On hand', 'Latest price', 'Stock value', ...(canManageItems ? ['Actions'] : [])], rows, 'No active items in this category.')}</section>`;
}

function renderItems() {
  const query = state.itemQuery.toLowerCase();
  const filter = $('#items-category-filter')?.value || 'ALL';
  const rows = state.data.items.filter(item => (item.active || state.showArchived) && (filter === 'ALL' || item.category === filter) && `${item.name} ${item.supplier_item_code} ${item.supplier_name || ''}`.toLowerCase().includes(query));
  const editActions = ['master', 'admin'].includes(state.user.role);
  const tableRows = rows.map(item => `<tr><td>${itemCell(item)}</td><td>${esc(item.category)}</td><td>${esc(item.supplier_name || '—')}</td><td>${esc(packLabel(item))}</td><td>${money(item.last_price ?? item.default_price)}</td><td class="${Number(item.on_hand_base) <= Number(item.reorder_level) ? 'quantity-low' : ''}">${esc(balanceLabel(item))}</td><td>${number(item.reorder_level)} ${esc(item.base_unit)}</td><td>${item.active ? '<span class="tag">Active</span>' : '<span class="tag inactive">Archived</span>'}</td>${editActions ? `<td><div class="table-actions"><button class="icon-button" data-edit-item="${item.id}" title="Edit item" aria-label="Edit ${esc(item.name)}">✎</button>${item.active ? `<button class="icon-button" data-adjust-item="${item.id}" title="Correct stock" aria-label="Correct stock for ${esc(item.name)}">±</button><button class="icon-button" data-archive-item="${item.id}" title="Archive item" aria-label="Archive ${esc(item.name)}">⌫</button>` : ''}</div></td>` : ''}</tr>`);
  const action = editActions ? '<button class="button button-primary" data-action="add-item">＋ Add item</button>' : '';
  return `${heading('PRODUCT CATALOGUE', 'Items', 'Maintain product details and see current quantities.', action)}
    <div class="toolbar"><input id="items-search" type="search" placeholder="Search item name, code or supplier" value="${esc(state.itemQuery)}" aria-label="Search items"><select class="filter-select" id="items-category-filter" aria-label="Filter by category">${categoryOptions(filter)}</select>${editActions ? `<button class="button button-secondary" id="archived-toggle">${state.showArchived ? 'Hide archived' : 'Show archived'}</button>` : ''}</div>
    <section class="section-panel table-panel">${table(['Item / supplier code', 'Category', 'Supplier', 'Package conversion', 'Latest price', 'On hand', 'Reorder at', 'Status', ...(editActions ? ['Actions'] : [])], tableRows, 'No matching items. Add a product to begin.')}</section>
    <div class="uom-note"><b>Package examples:</b> kg = 1 × 5 kg · gram = 1 × 0.5 gram · pieces = 1 × 1 piece · box = 6 × 6 = 36 pieces. Both package factors can be configured per item.</div>`;
}

function renderSuppliers() {
  const canEdit = ['master', 'admin'].includes(state.user.role);
  const rows = state.data.suppliers.map(supplier => `<tr><td><strong>${esc(supplier.name)}</strong></td><td>${esc(supplier.address || '—')}</td><td>${esc(supplier.telephone || '—')}</td><td>${esc(supplier.mobile || '—')}</td>${canEdit ? `<td><div class="table-actions"><button class="icon-button" data-edit-supplier="${supplier.id}" title="Edit supplier" aria-label="Edit ${esc(supplier.name)}">✎</button><button class="icon-button" data-delete-supplier="${supplier.id}" title="Delete supplier" aria-label="Delete ${esc(supplier.name)}">⌫</button></div></td>` : ''}</tr>`);
  return `${heading('PARTNER DIRECTORY', 'Suppliers', 'Keep supplier contacts close to your purchasing records.', canEdit ? '<button class="button button-primary" data-action="add-supplier">＋ Add supplier</button>' : '')}
    <section class="section-panel table-panel">${table(['Supplier name', 'Address', 'Telephone', 'Mobile', ...(canEdit ? ['Actions'] : [])], rows, 'No suppliers added yet.')}</section>`;
}

function movementFields(kind) {
  const isReceipt = kind === 'stock';
  const data = state.data;
  return `<form class="entry-form" data-form="${kind}">
    ${itemSearchField()}
    <div class="form-row"><label>Supplier item code<input name="supplier_item_code" readonly placeholder="Select an item"></label><label>Product unit<input name="uom" readonly placeholder="Select an item"></label></div>
    <div class="form-row"><label>Pack conversion<input name="pack_size" readonly placeholder="Select an item"></label>${isReceipt ? `<label>Supplier<select name="supplier_id">${supplierOptions()}</select></label>` : `<label>Recorded by<input readonly value="${esc(state.user.username)}"></label>`}</div>
    ${isReceipt ? `<label>Purchase order<select name="purchase_order_id">${orderOptions()}</select></label>` : ''}
    ${isReceipt ? '<label>Invoice number<input name="invoice_number" type="text" maxlength="100" autocomplete="off"></label>' : ''}
    <div class="form-row"><label>${isReceipt ? 'Packages received' : 'Packages issued'}<input name="quantity" type="number" min="0.001" step="any" required><small data-conversion-total></small></label>${isReceipt ? `<label>Price / package (${esc(data.currency)})<input name="unit_price" type="number" min="0" step="0.01" value="0" required></label>` : '<label>Stock out date<input name="movement_date" type="date" value="' + today() + '" required></label>'}</div>
    ${isReceipt ? `<label>Receipt date<input name="movement_date" type="date" value="${today()}" required></label><div class="form-total"><span>Total amount</span><b data-total>${money(0)}</b></div>` : ''}
    <button class="button button-primary" type="submit">${isReceipt ? 'Save stock receipt' : 'Record stock out'}</button>
  </form>`;
}

function renderReceiving() {
  const rows = state.data.movements.filter(row => row.movement_type === 'stock_in').map(row => `<tr><td>${esc(row.movement_date)}</td><td>${itemCell(row)}</td><td>${esc(row.supplier_name || '—')}</td><td>${esc(row.invoice_number || '—')}</td><td>${packageQuantityLabel(row.quantity, row.base_quantity, row.base_unit)}</td><td>${money(row.unit_price)}</td><td>${money(row.total_amount)}</td><td>${esc(row.username)}</td></tr>`);
  return `${heading('PURCHASING', 'Stock in', 'Record received goods, supplier and the price paid.')}
    <div class="split-layout"><section class="section-panel form-panel"><h2>Receive inventory</h2><p>Receipts add quantities to current stock and update the latest purchase price.</p>${movementFields('stock')}</section>
    <section class="section-panel table-panel"><div class="panel-heading"><h2>Recent receipts</h2><small>Latest 12 entries</small></div>${table(['Date', 'Item', 'Supplier', 'Invoice number', 'Received (packages and base unit)', 'Price / unit', 'Total', 'User'], rows, 'No stock receipts yet.')}</section></div>`;
}

function renderIssues() {
  const rows = state.data.movements.filter(row => row.movement_type === 'stock_out').map(row => `<tr><td>${esc(row.movement_date)}</td><td>${itemCell(row)}</td><td>${packageQuantityLabel(row.quantity, row.base_quantity, row.base_unit)}</td><td>${esc(row.category)}</td><td>${esc(row.username)}</td></tr>`);
  return `${heading('INVENTORY MOVEMENTS', 'Stock out', 'Record stock issued from the available quantity.')}
    <div class="split-layout"><section class="section-panel form-panel"><h2>Issue inventory</h2><p>Issue quantities cannot exceed the item’s current balance.</p>${movementFields('issue')}</section>
    <section class="section-panel table-panel"><div class="panel-heading"><h2>Recent stock out</h2><small>Latest 12 entries</small></div>${table(['Date', 'Item', 'Quantity', 'Category', 'User'], rows, 'No stock issues yet.')}</section></div>`;
}

function renderOrders() {
  const canManage = ['master', 'admin'].includes(state.user.role);
  const rows = state.data.orders.map(row => `<tr><td>#${row.id} · ${esc(row.order_date)}</td><td>${itemCell(row)}</td><td>${esc(row.supplier_name || '—')}</td><td>${packageQuantityLabel(row.quantity, row.quantity * row.pack_size, row.base_unit)}</td><td>${number(row.received_quantity)} / ${number(row.quantity)} packages</td><td>${packageQuantityLabel(row.remaining_quantity, row.remaining_quantity * row.pack_size, row.base_unit)}</td><td><span class="tag ${row.status === 'cancelled' ? 'inactive' : ''}">${esc(row.status)}</span></td><td>${esc(row.username)}</td>${canManage && ['open', 'partial'].includes(row.status) ? `<td><button class="button button-danger button-small" data-cancel-order="${row.id}">Cancel</button></td>` : canManage ? '<td>—</td>' : ''}</tr>`);
  return `${heading('PURCHASING', 'Purchase orders', 'Keep a dated record of planned item quantities.')}
    <div class="split-layout"><section class="section-panel form-panel"><h2>New purchase order</h2><p>Order records are separate from received stock.</p><form class="entry-form" data-form="order">
    ${itemSearchField()}<div class="form-row"><label>Supplier item code<input name="supplier_item_code" readonly></label><label>Product unit<input name="uom" readonly></label></div>
    <label>Pack conversion<input name="pack_size" readonly></label><label>Supplier<select name="supplier_id">${supplierOptions()}</select></label><div class="form-row"><label>Purchase order quantity (packages)<input name="quantity" type="number" min="0.001" step="any" required></label><label>Order date<input name="order_date" type="date" value="${today()}" required></label></div>
    <label>Ordered by<input readonly value="${esc(state.user.username)}"></label><button class="button button-primary" type="submit">Save purchase order</button></form></section>
    <section class="section-panel table-panel"><div class="panel-heading"><h2>Purchase order register</h2><small>Up to 100 most recent</small></div>${table(['Order / date', 'Item', 'Supplier', 'Ordered', 'Received', 'Remaining', 'Status', 'Ordered by', ...(canManage ? ['Actions'] : [])], rows, 'No purchase orders yet.')}</section></div>`;
}

function renderReports() {
  const actions = `<button class="button button-secondary" type="button" id="print-report">▣ Print preview</button><a class="button button-primary" id="download-report-pdf" href="${reportPdfUrl()}">↓ Download PDF</a>`;
  return `${heading('ANALYSIS', 'Inventory report', reportDescription(), actions, 'report-heading')}
    <form class="report-controls" id="report-form" data-form="reports"><label>From<input type="date" name="from" value="${esc(state.reportFrom || monthStart())}" required></label><label>To<input type="date" name="to" value="${esc(state.reportTo || today())}" required></label><label>Category<select name="category">${categoryOptions(state.reportCategory)}</select></label><label>Movement<select name="movement_type"><option value="ALL" ${state.reportMovementType === 'ALL' ? 'selected' : ''}>Stock in and out</option><option value="stock_in" ${state.reportMovementType === 'stock_in' ? 'selected' : ''}>Stock in only</option><option value="stock_out" ${state.reportMovementType === 'stock_out' ? 'selected' : ''}>Stock out only</option></select></label><button class="button button-primary" type="submit">Run report</button></form>
    <div id="report-results"><div class="loading-state">Loading report…</div></div>`;
}

function renderUsers() {
  if (!['master', 'admin'].includes(state.user.role)) {
    state.view = 'overview';
    return renderOverview();
  }
  const rows = state.data.users.map(user => `<tr><td><strong>${esc(user.username)}</strong>${user.id === state.user.id ? ' <small>(you)</small>' : ''}</td><td><select class="user-role-select" data-user-role="${user.id}" ${user.id === state.user.id || (state.user.role !== 'master' && user.role === 'master') ? 'disabled' : ''}>${['master', 'admin', 'staff'].filter(role => role !== 'master' || state.user.role === 'master' || user.role === 'master').map(role => `<option value="${role}" ${role === user.role ? 'selected' : ''}>${roleLabel(role)}</option>`).join('')}</select></td><td><span class="tag ${user.active ? '' : 'inactive'}">${user.active ? 'Active' : 'Inactive'}</span></td><td><div class="table-actions">${user.id !== state.user.id && (state.user.role === 'master' || user.role !== 'master') ? `<button class="button button-secondary button-small" data-reset-user="${user.id}">Set password</button><button class="button button-secondary button-small" data-toggle-user="${user.id}" data-active="${user.active}">${user.active ? 'Deactivate' : 'Activate'}</button>` : ''}</div></td></tr>`);
  return `${heading('ACCESS CONTROL', 'Users', 'Manage staff logins and account access.', '<button class="button button-primary" data-action="add-user">＋ Add user</button>')}
    <section class="section-panel table-panel">${table(['Username', 'Role', 'Status', 'Actions'], rows, 'No user accounts found.')}</section>
    <div class="uom-note">Only master and administrator accounts can create users or change access. Passwords are stored as secure hashes.</div>`;
}

function renderSettings() {
  if (!['master', 'admin'].includes(state.user.role)) return renderOverview();
  const categoryRows = state.data.categories.map(category => {
    const categoryId = state.data.categoryRecords?.find(record => record.name === category)?.id;
    return `<form class="settings-row" data-form="category-edit" data-category-id="${categoryId || ''}"><input name="name" value="${esc(category)}" required><button class="button button-secondary button-small" type="submit">Rename</button><button class="button button-danger button-small" type="button" data-delete-category="${categoryId || ''}">Delete</button></form>`;
  }).join('');
  const unitRows = state.data.uoms.map(unit => `<div class="settings-row"><span><b>${esc(unit.name)}</b><small>${number(unit.default_pack_count)} × ${number(unit.default_pack_size)} ${esc(unit.pack_unit)} → ${esc(unit.base_unit)}</small></span><button class="button button-danger button-small" data-delete-unit="${unit.id}">Delete</button></div>`).join('');
  const auditRows = state.data.audit.slice(0, 60).map(event => `<tr><td>${esc(event.created_at)}</td><td>${esc(event.username || 'System')}</td><td>${esc(event.action)}</td><td>${esc(event.entity_type)}${event.entity_id ? ` #${event.entity_id}` : ''}</td><td>${esc(event.details)}</td></tr>`);
  const drivePanel = state.user.role === 'master' && state.data.driveSyncSupported ? `<section class="section-panel form-panel drive-panel"><h2>Google Drive sync</h2><p>SQLite remains available on this server while offline. Master can connect Drive; successful changes upload a consistent database snapshot automatically when online.</p>
      <div id="drive-status"><div class="loading-state">Checking Drive connection…</div></div>
      <form class="entry-form drive-client-form" data-form="drive-client"><label>Google OAuth Web client JSON<input type="file" name="client_json" accept="application/json,.json" required></label><span class="field-hint">Create a Web application OAuth client in Google Cloud Console and add the callback URL shown above.</span><button class="button button-secondary" type="submit">Save OAuth client</button></form>
    </section>` : state.user.role === 'master' ? `<section class="section-panel form-panel"><h2>Database backups</h2><p>This deployment uses Turso’s hosted database. Google Drive background sync is unavailable on Vercel; manage database backups from your Turso dashboard.</p></section>` : '';
  const profile = state.data.businessProfile;
  return `${heading('WORKSPACE ADMINISTRATION', 'Settings & data', 'Configure units and categories, protect your database, and review changes.', '<button class="button button-secondary" data-action="change-password">Change my password</button>')}
    <div class="settings-grid">
      <section class="section-panel form-panel"><h2>Currency</h2><p>Set the currency before recording stock purchases. It is locked afterward to preserve totals.</p><form class="entry-form" data-form="settings"><label>Display currency<select name="currency">${['INR','LKR','USD','AED','BHD','EUR','GBP','AUD','CAD'].map(currency => `<option value="${currency}" ${state.data.currency === currency ? 'selected' : ''}>${currency}</option>`).join('')}</select></label><button class="button button-primary" type="submit">Save currency</button></form></section>
      <section class="section-panel form-panel"><h2>Shop details for reports</h2><p>These details appear at the top of printed and downloaded A4 reports. Logo must be PNG or JPEG, at most 1 MB.</p><form class="entry-form" data-form="business-profile" enctype="multipart/form-data">
        <label>Full shop name<input name="shop_name" maxlength="120" value="${esc(profile.shop_name)}" required></label>
        <label>Address<textarea name="address" rows="2" maxlength="300">${esc(profile.address)}</textarea></label>
        <div class="form-row"><label>Telephone<input name="telephone" maxlength="40" value="${esc(profile.telephone)}"></label><label>Mobile number<input name="mobile" maxlength="40" value="${esc(profile.mobile)}"></label></div>
        <label>Shop logo<input name="logo" type="file" accept="image/png,image/jpeg,.png,.jpg,.jpeg"></label>
        ${profile.has_logo ? `<div class="business-logo-preview"><img src="${esc(profile.logo_url)}" alt="Current shop logo"><label><input name="remove_logo" type="checkbox" value="1"> Remove current logo</label></div>` : ''}
        <button class="button button-primary" type="submit">Save shop details</button></form></section>
      <section class="section-panel form-panel"><h2>Database backup</h2><p>Download a consistent SQLite snapshot or export inventory data to CSV.</p><div class="settings-downloads"><a class="button button-primary" href="/api/backup">↓ SQLite backup</a><a class="button button-secondary" href="/api/export/items.csv">↓ Items CSV</a><a class="button button-secondary" href="/api/export/movements.csv">↓ Movements CSV</a></div></section>
      <section class="section-panel form-panel"><h2>Categories</h2><p>Renaming a category updates linked products and history.</p><div class="settings-list">${categoryRows}</div><form class="settings-row" data-form="category"><input name="name" placeholder="New category" required><button class="button button-primary button-small" type="submit">Add</button></form></section>
      <section class="section-panel form-panel"><h2>Product units</h2><p>Define the package and base-unit conversion used for new products.</p><div class="settings-list">${unitRows}</div><form class="entry-form settings-unit-form" data-form="unit"><label>Unit name<input name="name" required maxlength="32" placeholder="e.g. bundle"></label><div class="form-row"><label>Packs per package<input name="default_pack_count" type="number" min="1" step="1" value="1" required></label><label>Units per pack<input name="default_pack_size" type="number" min="0.001" step="any" required></label></div><div class="form-row"><label>Pack label<input name="pack_unit" required placeholder="e.g. pieces"></label><label>Base unit<input name="base_unit" required placeholder="e.g. pieces"></label></div><button class="button button-primary" type="submit">Add unit</button></form></section>
      ${drivePanel}
    </div>
    <h2 class="report-section-title">Recent audit history</h2><section class="section-panel table-panel">${table(['Time', 'User', 'Action', 'Record', 'Details'], auditRows, 'Changes will be recorded here.')}</section>`;
}

async function loadDriveStatus() {
  const container = $('#drive-status');
  if (!container) return;
  try {
    const status = await api('/api/drive/status');
    if (!$('#drive-status')) return;
    const stateLabel = status.connected
      ? status.needs_initial_choice ? 'Connected · existing backup needs a choice' : status.pending ? 'Connected · changes waiting to sync' : 'Connected · up to date'
      : status.configured ? 'OAuth client saved · connect Google Drive' : 'Not connected';
    const initialChoice = status.needs_initial_choice
      ? '<div class="drive-warning">A BT INVO backup already exists in Google Drive. Starting sync will replace it with this device’s database.</div>'
      : '';
    const controls = status.connected ? `<div class="drive-controls">${status.needs_initial_choice ? '' : `<label class="drive-toggle"><input class="drive-auto-sync" type="checkbox" ${status.enabled ? 'checked' : ''}> Automatic sync</label>`}<button class="button button-primary" id="drive-sync-button">${status.needs_initial_choice ? 'Use this database and replace Drive copy' : 'Sync now'}</button>${status.backup_exists ? '<a class="button button-secondary" href="/api/drive/backup">↓ Download Drive backup</a>' : ''}<button class="button button-danger button-small" id="drive-disconnect-button">Disconnect</button></div>`
      : status.configured ? '<a class="button button-primary" href="/api/drive/connect">Connect Google Drive</a>' : '';
    container.innerHTML = `<div class="drive-status-row"><span class="status-dot ${status.connected && !status.pending ? 'connected' : ''}"></span><b>${esc(stateLabel)}</b></div>${status.last_sync ? `<p class="drive-last-sync">Last successful upload: ${esc(status.last_sync)}</p>` : ''}${status.last_error ? `<p class="drive-error">${esc(status.last_error)}</p>` : ''}${initialChoice}${controls}<p class="drive-callback">Google OAuth authorized redirect URI: <code>${esc(status.callback_url)}</code></p><p class="drive-footnote">Drive sync stores a private backup in this app’s Google Drive data folder. Offline work stays in SQLite and retries automatically about once a minute when connectivity returns.</p>`;
  } catch (error) {
    container.innerHTML = `<p class="drive-error">${esc(error.message)}</p>`;
  }
}

async function loadReport() {
  const container = $('#report-results');
  if (!container) return;
  state.reportFrom ||= monthStart();
  state.reportTo ||= today();
  try {
    const query = new URLSearchParams({ from: state.reportFrom, to: state.reportTo, category: state.reportCategory, movement_type: state.reportMovementType });
    state.report = await api(`/api/reports?${query}`);
    if (!$('#report-results')) return;
    const report = state.report;
    const total = report.totals;
    const movementRows = report.movements.map(row => `<tr><td>${esc(row.movement_date)}</td><td>${itemCell(row)}</td><td>${esc(row.category)}</td><td><span class="tag ${row.movement_type === 'stock_out' ? 'inactive' : ''}">${row.movement_type === 'stock_in' ? 'Stock in' : 'Stock out'}</span></td><td>${packageQuantityLabel(row.quantity, row.base_quantity, row.base_unit)}</td><td>${money(row.report_amount)}${row.movement_type === 'stock_out' ? ' estimated' : ''}</td><td>${esc(row.username)}</td></tr>`);
    const orderRows = report.orders.map(row => `<tr><td>${esc(row.order_date)}</td><td>${itemCell(row)}</td><td>${esc(row.category)}</td><td>${packageQuantityLabel(row.quantity, row.quantity * row.pack_size, row.base_unit)}</td><td>${esc(row.username)}</td></tr>`);
    const adjustmentRows = report.adjustments.map(row => `<tr><td>${esc(row.adjustment_date)}</td><td>${itemCell(row)}</td><td>${number(row.quantity_delta)} ${esc(row.uom)}</td><td>${number(row.base_quantity_delta)} ${esc(row.base_unit)}</td><td>${esc(row.reason)}</td><td>${esc(row.note)}</td><td>${esc(row.username)}</td></tr>`);
    const balanceRows = report.balances.map(row => `<tr><td>${itemCell(row)}</td><td>${esc(row.category)}</td><td>${number(row.opening_base)} ${esc(row.base_unit)}</td><td>${number(row.received_base)} ${esc(row.base_unit)}</td><td>${number(row.issued_base)} ${esc(row.base_unit)}</td><td>${number(row.adjustment_base)} ${esc(row.base_unit)}</td><td><strong>${number(row.closing_base)} ${esc(row.base_unit)}</strong></td><td>${number(row.closing_packs)} ${esc(row.uom)}</td></tr>`);
    const groupDailyMovements = type => {
      const groups = new Map();
      report.movements.filter(row => row.movement_type === type).forEach(row => {
        const key = `${row.item_id}:${row.movement_date}`;
        const current = groups.get(key) || { ...row, quantity_total: 0, base_quantity_total: 0, amount_total: 0, price_count: 0 };
        current.quantity_total += Number(row.quantity);
        current.base_quantity_total += Number(row.base_quantity);
        current.amount_total += Number(row.total_amount);
        current.price_count += 1;
        groups.set(key, current);
      });
      return [...groups.values()];
    };
    const purchaseSummary = groupDailyMovements('stock_in').map(row => `<tr><td>${esc(row.movement_date)}</td><td>${itemCell(row)}</td><td>${esc(row.category)}</td><td>${packageQuantityLabel(row.quantity_total, row.base_quantity_total, row.base_unit)}</td><td>${money(row.amount_total)}</td><td>${money(row.amount_total / row.quantity_total)}</td><td>${row.price_count}</td></tr>`);
    const issueSummary = groupDailyMovements('stock_out').map(row => `<tr><td>${esc(row.movement_date)}</td><td>${itemCell(row)}</td><td>${esc(row.category)}</td><td>${packageQuantityLabel(row.quantity_total, row.base_quantity_total, row.base_unit)}</td><td>${row.price_count}</td></tr>`);
    const movementLabel = state.reportMovementType === 'ALL' ? 'Stock in and out' : state.reportMovementType === 'stock_in' ? 'Stock in only' : 'Stock out only';
    const showPurchases = state.reportMovementType !== 'stock_out';
    const showIssues = state.reportMovementType !== 'stock_in';
    const showOtherSections = state.reportMovementType === 'ALL';
    const reportStats = showOtherSections
      ? `<div class="report-stats"><div class="report-stat"><small>Receipts</small><strong>${number(total.received_transactions)}</strong></div><div class="report-stat"><small>Purchase spend (${esc(state.data.currency)})</small><strong>${money(total.received_amount)}</strong></div><div class="report-stat"><small>Issue transactions</small><strong>${number(total.issued_transactions)}</strong></div><div class="report-stat"><small>Purchase orders</small><strong>${total.purchase_orders}</strong></div></div>`
      : state.reportMovementType === 'stock_in'
        ? `<div class="report-stats report-stats-focused"><div class="report-stat"><small>Receipts</small><strong>${number(total.received_transactions)}</strong></div><div class="report-stat"><small>Purchase spend (${esc(state.data.currency)})</small><strong>${money(total.received_amount)}</strong></div></div>`
        : `<div class="report-stats report-stats-focused"><div class="report-stat"><small>Issue transactions</small><strong>${number(total.issued_transactions)}</strong></div><div class="report-stat"><small>Estimated issue value (${esc(state.data.currency)})</small><strong>${money(report.movements.reduce((sum, row) => sum + Number(row.report_amount || 0), 0))}</strong></div></div>`;
    const profile = state.data.businessProfile;
    const contactDetails = [profile.address, profile.telephone ? `Telephone: ${profile.telephone}` : '', profile.mobile ? `Mobile: ${profile.mobile}` : ''].filter(Boolean);
    const reportMasthead = `<header class="report-masthead">${profile.has_logo ? `<img src="${esc(profile.logo_url)}" alt="Shop logo">` : ''}<div><h2>${esc(profile.shop_name)}</h2>${contactDetails.map(detail => `<p>${esc(detail)}</p>`).join('')}</div></header>`;
    const filterNote = showOtherSections
      ? 'Showing stock movements, corrections, purchase orders, and balances for the selected dates and category.'
      : `Showing only ${movementLabel.toLowerCase()} for the selected dates and category.`;
    container.innerHTML = `${reportMasthead}<div class="report-period"><strong>${esc(report.from)} – ${esc(report.to)}</strong><span>${esc(report.category === 'ALL' ? 'All categories' : report.category)} · ${movementLabel}</span></div><p class="report-filter-note">${filterNote}</p>${reportStats}
      ${showPurchases ? `<h2 class="report-section-title">Stock purchase summary <span class="field-hint">${esc(report.from)} to ${esc(report.to)}</span></h2><section class="section-panel table-panel">${table(['Date', 'Item', 'Category', 'Received', 'Amount spent', 'Avg. price / package', 'Receipts'], purchaseSummary, 'No stock purchases in this date range.')}</section>` : ''}
      ${showIssues ? `<h2 class="report-section-title">Stock out summary <span class="field-hint">${esc(report.from)} to ${esc(report.to)}</span></h2><section class="section-panel table-panel">${table(['Date', 'Item', 'Category', 'Issued', 'Issues'], issueSummary, 'No stock issues in this date range.')}</section>` : ''}
      ${showOtherSections ? `<h2 class="report-section-title">Stock corrections</h2><section class="section-panel table-panel">${table(['Date', 'Item', 'Change', 'Base change', 'Reason', 'Note', 'User'], adjustmentRows, 'No stock corrections in this date range.')}</section>` : ''}
      <h2 class="report-section-title">${state.reportMovementType === 'stock_in' ? 'Stock receipt details' : state.reportMovementType === 'stock_out' ? 'Stock issue details' : 'Stock movement details'}${state.reportMovementType === 'stock_out' ? ' <span class="field-hint">Issue amounts are estimated using the latest purchase price on or before each issue date</span>' : ''}</h2><section class="section-panel table-panel">${table(['Date', 'Item', 'Category', 'Movement', 'Quantity', 'Amount', 'User'], movementRows, 'No stock movements in this date range.')}</section>
      ${showOtherSections ? `<h2 class="report-section-title">Purchase orders</h2><section class="section-panel table-panel">${table(['Date', 'Item', 'Category', 'Quantity', 'Ordered by'], orderRows, 'No purchase orders in this date range.')}</section>
      <h2 class="report-section-title">Opening and closing stock by item</h2><section class="section-panel table-panel">${table(['Item', 'Category', 'Opening', 'Received', 'Issued', 'Corrections', 'Closing', 'Packages'], balanceRows, 'No stock balances in this date range.')}</section>` : ''}<div class="report-print-footer">Powered by JC007</div>`;
  } catch (error) {
    container.innerHTML = `<div class="empty-state">${esc(error.message)}</div>`;
  }
}

function openModal(title, subtitle, content) {
  $('#modal-content').innerHTML = `<div class="modal-head"><div><h2>${esc(title)}</h2><p>${esc(subtitle)}</p></div><button class="icon-button" data-close-modal aria-label="Close dialog">×</button></div><div class="modal-body">${content}</div>`;
  $('#modal').showModal();
}

function itemModal(item = null) {
  const editing = Boolean(item);
  const units = state.data.uoms;
  const unit = units.find(row => row.name === item?.uom) || units[0];
  openModal(editing ? 'Edit item' : 'Add item', 'Define the product and how one package converts to its base unit.', `<form class="modal-form" data-form="item" data-item-id="${item?.id || ''}">
    <label>Item name<input name="name" value="${esc(item?.name || '')}" required maxlength="160"></label>
    <div class="form-row"><label>Supplier item code<input name="supplier_item_code" value="${esc(item?.supplier_item_code || '')}"></label><label>Category<select name="category" required>${state.data.categories.map(category => `<option value="${esc(category)}" ${item?.category === category ? 'selected' : ''}>${esc(category)}</option>`).join('')}</select></label></div>
    <div class="form-row"><label>Product unit<select name="uom" required>${units.map(row => `<option value="${esc(row.name)}" ${row.name === unit.name ? 'selected' : ''}>${esc(row.name)}</option>`).join('')}</select></label><label>Inner packs in one package<input name="pack_count" type="number" min="1" step="1" value="${item?.pack_count ?? unit.default_pack_count}" required></label></div>
    <div class="form-row"><label>Base quantity in each inner pack (${esc(item?.base_unit || unit.base_unit)})<input name="units_per_pack" type="number" min="0.001" step="any" value="${item?.units_per_pack ?? unit.default_pack_size}" required></label><label>Low-stock alert at (${esc(item?.base_unit || unit.base_unit)})<input name="reorder_level" type="number" min="0" step="any" value="${item?.reorder_level ?? 5}" required></label></div>
    <div class="form-row"><label>Price / package (${esc(state.data.currency)})<input name="default_price" type="number" min="0" step="0.01" value="${item?.default_price ?? item?.last_price ?? 0}" required></label><label>Supplier<select name="supplier_id">${supplierOptions(item?.supplier_id || '')}</select></label></div>
    <div class="conversion-guide" data-item-conversion></div>
    <p class="field-hint">This form defines one package only. Enter the number of packages when recording Stock in; the app converts that amount to the base unit.</p>
    <div class="modal-actions"><button class="button button-secondary" type="button" data-close-modal>Cancel</button><button class="button button-primary" type="submit">${editing ? 'Save changes' : 'Add item'}</button></div></form>`);
  updateItemConversion($('#modal-content form[data-form="item"]'));
}

function updateItemConversion(form) {
  const guide = $('[data-item-conversion]', form);
  const unit = state.data.uoms.find(row => row.name === form.elements.uom.value);
  if (!guide || !unit) return;
  const count = Number(form.elements.pack_count.value);
  const quantityPerPack = Number(form.elements.units_per_pack.value);
  const packageQuantity = count * quantityPerPack;
  const baseUnit = unit.base_unit || unit.pack_unit;
  const sampleReceived = 10;
  guide.innerHTML = `<strong>Package conversion</strong><span>1 package = ${number(count)} inner pack${count === 1 ? '' : 's'} × ${number(quantityPerPack)} ${esc(baseUnit)} = <b>${number(packageQuantity)} ${esc(baseUnit)}</b></span><span>Example: receive ${sampleReceived} packages → <b>${number(sampleReceived * packageQuantity)} ${esc(baseUnit)}</b> added to stock.</span>`;
}

function supplierModal(supplier = null) {
  const editing = Boolean(supplier);
  openModal(editing ? 'Edit supplier' : 'Add supplier', 'Save the supplier contact information.', `<form class="modal-form" data-form="supplier" data-supplier-id="${supplier?.id || ''}">
    <label>Supplier name<input name="name" value="${esc(supplier?.name || '')}" required></label><label>Address<input name="address" value="${esc(supplier?.address || '')}"></label>
    <div class="form-row"><label>Telephone<input name="telephone" value="${esc(supplier?.telephone || '')}"></label><label>Mobile<input name="mobile" value="${esc(supplier?.mobile || '')}"></label></div>
    <div class="modal-actions"><button class="button button-secondary" type="button" data-close-modal>Cancel</button><button class="button button-primary" type="submit">${editing ? 'Save changes' : 'Add supplier'}</button></div></form>`);
}

function userModal(user = null) {
  const editing = Boolean(user);
  const canCreateMaster = state.user.role === 'master';
  openModal(editing ? 'Set user password' : 'Add user', editing ? `Set a new password for ${user.username}.` : 'Create login credentials for a team member.', `<form class="modal-form" data-form="user" data-user-id="${user?.id || ''}">
    ${editing ? '' : '<label>Username<input name="username" required maxlength="160" autocomplete="off"></label>'}<label>${editing ? 'New password' : 'Password'}<input name="password" type="password" required minlength="12" maxlength="256" autocomplete="new-password"></label>
    ${editing ? '' : `<label>Role<select name="role" required><option value="staff">Staff</option><option value="admin">Administrator</option>${canCreateMaster ? '<option value="master">Master user</option>' : ''}</select></label>`}
    <div class="modal-actions"><button class="button button-secondary" type="button" data-close-modal>Cancel</button><button class="button button-primary" type="submit">${editing ? 'Update password' : 'Create account'}</button></div></form>`);
}

function adjustmentModal(item) {
  openModal('Correct stock balance', `${item.name} · current balance ${balanceLabel(item)}`, `<form class="modal-form" data-form="adjustment" data-item-id="${item.id}">
    <label>Quantity change (${esc(item.uom)})<input name="quantity_delta" type="number" step="any" required placeholder="Use minus for a decrease"></label>
    <label>Reason<select name="reason" required><option value="count correction">Count correction</option><option value="damage">Damage or spoilage</option><option value="opening balance">Opening balance</option><option value="other">Other</option></select></label>
    <label>Adjustment date<input name="adjustment_date" type="date" value="${today()}" required></label><label>Note<input name="note" maxlength="500" placeholder="Optional explanation"></label>
    <div class="modal-actions"><button class="button button-secondary" type="button" data-close-modal>Cancel</button><button class="button button-primary" type="submit">Save correction</button></div></form>`);
}

function accountPasswordModal(required = false) {
  openModal(required ? 'Set a new password to continue' : 'Change my password', required ? 'This account must set a new password before using the app.' : 'Use your current password to confirm the change.', `<form class="modal-form" data-form="account-password">
    <label>Current password<input name="old_password" type="password" autocomplete="current-password" required></label>
    <label>New password<input name="new_password" type="password" autocomplete="new-password" minlength="12" maxlength="256" required></label>
    <div class="modal-actions">${required ? '' : '<button class="button button-secondary" type="button" data-close-modal>Cancel</button>'}<button class="button button-primary" type="submit">Update password</button></div></form>`);
}

function itemMeta(form, item) {
  if (!item) return;
  form.elements.supplier_item_code.value = item.supplier_item_code || '';
  form.elements.uom.value = item.uom;
  form.elements.pack_size.value = packLabel(item);
  setPackageConversion(form, item.pack_size, item.base_unit);
  if (form.elements.supplier_id) form.elements.supplier_id.value = item.supplier_id || '';
  if (form.elements.unit_price) form.elements.unit_price.value = Number(item.last_price ?? item.default_price ?? 0);
  updateTotal(form);
}

function updateTotal(form) {
  const total = $('[data-total]', form);
  if (total) total.textContent = money(Number(form.elements.quantity.value || 0) * Number(form.elements.unit_price.value || 0));
}

function setPackageConversion(form, packSize, baseUnit) {
  const total = $('[data-conversion-total]', form);
  if (!total) return;
  total.dataset.packSize = packSize;
  total.dataset.baseUnit = baseUnit;
  updateConversionTotal(form);
}

function updateConversionTotal(form) {
  const total = $('[data-conversion-total]', form);
  if (!total || !total.dataset.packSize || !total.dataset.baseUnit) return;
  const quantity = Number(form.elements.quantity.value || 0);
  total.textContent = `${form.dataset.form === 'stock' ? 'Stock added' : 'Stock issued'}: ${number(quantity * Number(total.dataset.packSize))} ${total.dataset.baseUnit}`;
}

async function submitForm(event) {
  const form = event.target;
  if (!form.matches('form[data-form]')) return;
  event.preventDefault();
  const kind = form.dataset.form;
  const values = Object.fromEntries(new FormData(form).entries());
  try {
    if (['stock', 'issue', 'order'].includes(kind)) {
      const item = getItemFromChoice(values.item_choice);
      if (!item) throw new Error('Choose an item from the search suggestions.');
      values.item_id = item.id;
      delete values.item_choice;
    }
    if (kind === 'stock') await api('/api/stock', { method: 'POST', body: JSON.stringify(values) });
    if (kind === 'issue') await api('/api/issues', { method: 'POST', body: JSON.stringify(values) });
    if (kind === 'order') await api('/api/purchase-orders', { method: 'POST', body: JSON.stringify(values) });
    if (kind === 'item') {
      const id = form.dataset.itemId;
      await api(id ? `/api/items/${id}` : '/api/items', { method: id ? 'PUT' : 'POST', body: JSON.stringify(values) });
    }
    if (kind === 'supplier') {
      const id = form.dataset.supplierId;
      await api(id ? `/api/suppliers/${id}` : '/api/suppliers', { method: id ? 'PUT' : 'POST', body: JSON.stringify(values) });
    }
    if (kind === 'user') {
      const id = form.dataset.userId;
      await api(id ? `/api/users/${id}` : '/api/users', { method: id ? 'PATCH' : 'POST', body: JSON.stringify(values) });
    }
    if (kind === 'adjustment') {
      await api(`/api/items/${form.dataset.itemId}/adjustments`, { method: 'POST', body: JSON.stringify(values) });
    }
    if (kind === 'category') await api('/api/categories', { method: 'POST', body: JSON.stringify(values) });
    if (kind === 'category-edit') await api(`/api/categories/${form.dataset.categoryId}`, { method: 'PUT', body: JSON.stringify(values) });
    if (kind === 'unit') await api('/api/units', { method: 'POST', body: JSON.stringify(values) });
    if (kind === 'settings') await api('/api/settings', { method: 'PATCH', body: JSON.stringify(values) });
    if (kind === 'drive-client') await api('/api/drive/client', { method: 'POST', body: new FormData(form) });
    if (kind === 'business-profile') await api('/api/business-profile', { method: 'POST', body: new FormData(form) });
    if (kind === 'account-password') await api('/api/account/password', { method: 'POST', body: JSON.stringify(values) });
    if (kind === 'reports') {
      state.reportFrom = values.from;
      state.reportTo = values.to;
      state.reportCategory = values.category;
      state.reportMovementType = values.movement_type;
      $('#download-report-pdf').href = reportPdfUrl();
      $('.report-heading p').textContent = reportDescription();
      loadReport();
      return;
    }
    $('#modal').close();
    await loadData();
    notify(({ stock: 'Stock receipt saved.', issue: 'Stock issue recorded.', order: 'Purchase order saved.', item: 'Item saved.', supplier: 'Supplier saved.', user: 'User account saved.', adjustment: 'Stock correction recorded.', category: 'Category added.', 'category-edit': 'Category renamed.', unit: 'Unit added.', settings: 'Settings saved.', 'drive-client': 'Google OAuth client saved securely.', 'business-profile': 'Shop details saved.', 'account-password': 'Password updated.' })[kind] || 'Saved.');
  } catch (error) {
    notify(error.message, true);
  }
}

document.addEventListener('submit', submitForm);

document.addEventListener('click', async event => {
  const viewButton = event.target.closest('[data-view]');
  if (viewButton && state.data) {
    event.preventDefault();
    state.view = viewButton.dataset.view;
    state.itemQuery = '';
    render();
    return;
  }
  if (event.target.closest('[data-close-modal]')) { $('#modal').close(); return; }
  if (event.target.closest('#print-report')) { window.print(); return; }
  if (event.target.closest('#drive-sync-button')) {
    const button = $('#drive-sync-button');
    button.disabled = true;
    try {
      await api('/api/drive/sync', { method: 'POST', body: JSON.stringify({}) });
      notify('Database snapshot queued for Google Drive.');
      setTimeout(loadDriveStatus, 1200);
    } catch (error) { notify(error.message, true); }
    finally { button.disabled = false; }
    return;
  }
  if (event.target.closest('#drive-disconnect-button') && confirm('Disconnect Google Drive? Existing backups in Drive will be kept.')) {
    try { await api('/api/drive/disconnect', { method: 'POST', body: JSON.stringify({}) }); await loadDriveStatus(); notify('Google Drive disconnected.'); } catch (error) { notify(error.message, true); }
    return;
  }
  const action = event.target.closest('[data-action]')?.dataset.action;
  if (action === 'add-stock') { state.view = 'receiving'; render(); return; }
  if (action === 'add-item') { itemModal(); return; }
  if (action === 'add-supplier') { supplierModal(); return; }
  if (action === 'add-user') { userModal(); return; }
  if (action === 'change-password') { accountPasswordModal(); return; }
  const editItem = event.target.closest('[data-edit-item]');
  if (editItem) { itemModal(state.data.items.find(item => item.id === Number(editItem.dataset.editItem))); return; }
  const adjustItem = event.target.closest('[data-adjust-item]');
  if (adjustItem) { adjustmentModal(state.data.items.find(item => item.id === Number(adjustItem.dataset.adjustItem))); return; }
  const archive = event.target.closest('[data-archive-item]');
  if (archive && confirm('Archive this item? Its movement history will be kept.')) {
    try { await api(`/api/items/${archive.dataset.archiveItem}`, { method: 'DELETE' }); await loadData(); notify('Item archived.'); } catch (error) { notify(error.message, true); }
    return;
  }
  const editSupplier = event.target.closest('[data-edit-supplier]');
  if (editSupplier) { supplierModal(state.data.suppliers.find(supplier => supplier.id === Number(editSupplier.dataset.editSupplier))); return; }
  const deleteSupplier = event.target.closest('[data-delete-supplier]');
  if (deleteSupplier && confirm('Delete this supplier? Suppliers linked to inventory cannot be deleted.')) {
    try { await api(`/api/suppliers/${deleteSupplier.dataset.deleteSupplier}`, { method: 'DELETE' }); await loadData(); notify('Supplier deleted.'); } catch (error) { notify(error.message, true); }
    return;
  }
  const reset = event.target.closest('[data-reset-user]');
  if (reset) { userModal(state.data.users.find(user => user.id === Number(reset.dataset.resetUser))); return; }
  const toggle = event.target.closest('[data-toggle-user]');
  if (toggle) {
    try { await api(`/api/users/${toggle.dataset.toggleUser}`, { method: 'PATCH', body: JSON.stringify({ active: toggle.dataset.active !== '1' }) }); await loadData(); notify('User access updated.'); } catch (error) { notify(error.message, true); }
    return;
  }
  const cancelOrder = event.target.closest('[data-cancel-order]');
  if (cancelOrder && confirm('Cancel this purchase order? It cannot receive additional stock afterward.')) {
    try { await api(`/api/purchase-orders/${cancelOrder.dataset.cancelOrder}`, { method: 'PATCH', body: JSON.stringify({ status: 'cancelled' }) }); await loadData(); notify('Purchase order cancelled.'); } catch (error) { notify(error.message, true); }
    return;
  }
  const deleteCategory = event.target.closest('[data-delete-category]');
  if (deleteCategory && confirm('Delete this category? Categories assigned to products cannot be deleted.')) {
    try { await api(`/api/categories/${deleteCategory.dataset.deleteCategory}`, { method: 'DELETE' }); await loadData(); notify('Category deleted.'); } catch (error) { notify(error.message, true); }
    return;
  }
  const deleteUnit = event.target.closest('[data-delete-unit]');
  if (deleteUnit && confirm('Delete this unit? Units assigned to products cannot be deleted.')) {
    try { await api(`/api/units/${deleteUnit.dataset.deleteUnit}`, { method: 'DELETE' }); await loadData(); notify('Unit deleted.'); } catch (error) { notify(error.message, true); }
    return;
  }
  if (event.target.closest('#archived-toggle')) { state.showArchived = !state.showArchived; render(); return; }
  const logoutButton = event.target.closest('#logout-button');
  if (logoutButton) {
    logoutButton.disabled = true;
    try {
      await api('/api/logout', { method: 'POST' });
    } catch {
      notify('Could not confirm sign out. Check your connection and try again.', true);
      logoutButton.disabled = false;
      return;
    }
    state.data = null;
    state.user = null;
    $('#login-form').reset();
    logoutButton.disabled = false;
    showLogin();
  }
});

document.addEventListener('change', async event => {
  if (event.target.matches('.drive-auto-sync')) {
    try {
      const result = await api('/api/drive/auto-sync', { method: 'PATCH', body: JSON.stringify({ enabled: event.target.checked }) });
      notify(result.enabled ? 'Automatic Drive sync enabled.' : 'Automatic Drive sync paused.');
      await loadDriveStatus();
    } catch (error) { notify(error.message, true); await loadDriveStatus(); }
    return;
  }
  if (event.target.id === 'overview-category') { state.category = event.target.value; render(); return; }
  if (event.target.id === 'current-stock-category') { state.currentStockCategory = event.target.value; render(); return; }
  if (event.target.id === 'items-category-filter') { render(); return; }
  if (event.target.matches('.entry-form [name="item_choice"]')) {
    const item = getItemFromChoice(event.target.value);
    if (item) {
      itemMeta(event.target.form, item);
      const orderSelect = event.target.form.elements.purchase_order_id;
      if (orderSelect) orderSelect.innerHTML = orderOptions(item.id);
    }
  }
  if (event.target.matches('.entry-form [name="purchase_order_id"]')) {
    const order = state.data.orders.find(candidate => candidate.id === Number(event.target.value));
    if (order) {
      const item = activeItems().find(candidate => candidate.id === order.item_id);
      if (item) {
        event.target.form.elements.item_choice.value = itemChoice(item);
        itemMeta(event.target.form, item);
        if (event.target.form.elements.supplier_id) event.target.form.elements.supplier_id.value = order.supplier_id || item.supplier_id || '';
        event.target.form.elements.pack_size.value = `${number(order.pack_count)} × ${number(order.units_per_pack)} = ${number(order.pack_size)} ${order.base_unit}`;
        setPackageConversion(event.target.form, order.pack_size, order.base_unit);
      }
      event.target.form.elements.quantity.max = order.remaining_quantity;
    } else event.target.form.elements.quantity.removeAttribute('max');
  }
  if (event.target.matches('.modal-form [name="uom"]')) {
    const unit = state.data.uoms.find(row => row.name === event.target.value);
    if (unit) {
      event.target.form.elements.pack_count.value = unit.default_pack_count;
      event.target.form.elements.units_per_pack.value = unit.default_pack_size;
      event.target.form.elements.units_per_pack.labels[0].childNodes[0].textContent = `Base quantity in each inner pack (${unit.base_unit})`;
      event.target.form.elements.reorder_level.labels[0].childNodes[0].textContent = `Low-stock alert at (${unit.base_unit})`;
      updateItemConversion(event.target.form);
    }
  }
  if (event.target.matches('.user-role-select')) {
    try { await api(`/api/users/${event.target.dataset.userRole}`, { method: 'PATCH', body: JSON.stringify({ role: event.target.value }) }); await loadData(); notify('User role updated.'); } catch (error) { notify(error.message, true); render(); }
  }
});

document.addEventListener('input', event => {
  if (event.target.id === 'items-search') { state.itemQuery = event.target.value; const position = event.target.selectionStart; render(); const input = $('#items-search'); input.focus(); input.setSelectionRange(position, position); }
  if (event.target.matches('.modal-form[data-form="item"] [name="pack_count"], .modal-form[data-form="item"] [name="units_per_pack"]')) updateItemConversion(event.target.form);
  if (event.target.matches('.entry-form [name="quantity"], .entry-form [name="unit_price"]')) {
    updateTotal(event.target.form);
    updateConversionTotal(event.target.form);
  }
  if (event.target.matches('.entry-form [name="item_choice"]')) {
    const item = getItemFromChoice(event.target.value);
    if (item) {
      itemMeta(event.target.form, item);
      const orderSelect = event.target.form.elements.purchase_order_id;
      if (orderSelect) orderSelect.innerHTML = orderOptions(item.id);
    }
  }
});

$('#login-form').addEventListener('submit', async event => {
  event.preventDefault();
  const values = Object.fromEntries(new FormData(event.currentTarget).entries());
  $('#login-error').textContent = '';
  try {
    const result = await api('/api/login', { method: 'POST', body: JSON.stringify(values) });
    if (result.user.password_change_required) accountPasswordModal(true);
    else await loadData();
  }
  catch (error) { $('#login-error').textContent = error.message; }
});

async function start() {
  try { await loadData(); }
  catch { showLogin(); }
}

start();