const $ = s => document.querySelector(s);
const state = { jobId: null, job: null, timer: null };
const fmtBytes = n => n < 1024 ? `${n} B` : n < 1048576 ? `${(n/1024).toFixed(1)} KB` : `${(n/1048576).toFixed(1)} MB`;
const fmtDate = n => n ? new Date(n*1000).toLocaleString('vi-VN') : '—';
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

function toast(message){const el=$('#toast');el.textContent=message;el.classList.add('show');setTimeout(()=>el.classList.remove('show'),2800)}

document.querySelectorAll('.nav').forEach(btn=>btn.onclick=()=>{
  document.querySelectorAll('.nav,.view').forEach(el=>el.classList.remove('active'));
  btn.classList.add('active'); $(`#${btn.dataset.view}`).classList.add('active');
  if(btn.dataset.view==='history') loadHistory();
});

$('#crawl-form').onsubmit=async e=>{
  e.preventDefault();
  $('#start-btn').disabled=true;
  const payload={url:$('#url').value,engine:$('#engine').value,scope:$('#scope').value,language:$('#language').value,use_sitemap:$('#use-sitemap').checked,max_pages:+$('#max-pages').value,timeout:+$('#timeout').value,crawl_links:$('#crawl-links').checked,keep_html:$('#keep-html').checked};
  try{
    const res=await fetch('/api/jobs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    const data=await res.json(); if(!res.ok) throw new Error(data.error);
    state.jobId=data.id; renderJob(data); poll(); toast('Đã khởi tạo phiên crawl');
  }catch(err){toast(err.message)}finally{$('#start-btn').disabled=false}
};

async function poll(){
  clearTimeout(state.timer); if(!state.jobId)return;
  try{const res=await fetch(`/api/jobs/${state.jobId}`);state.job=await res.json();renderJob(state.job);
    if(['queued','running'].includes(state.job.status))state.timer=setTimeout(poll,700);
  }catch{state.timer=setTimeout(poll,1800)}
}

function renderJob(job){
  const active=['queued','running'].includes(job.status), done=job.status==='completed';
  const percent=done?100:Math.min(99,Math.round((job.processed/Math.max(1,job.max_pages))*100));
  $('#progress-value').textContent=`${percent}%`;$('#progress-ring').style.setProperty('--progress',`${percent*3.6}deg`);
  $('#status-message').textContent=job.message;$('#current-url').textContent=job.current_url||job.url;
  $('#metric-success').textContent=job.successful;$('#metric-failed').textContent=job.failed;$('#metric-data').textContent=fmtBytes(job.bytes||0);
  const badge=$('#status-badge');badge.textContent=({queued:'Đang chờ',running:'Đang chạy',completed:'Hoàn tất',failed:'Có lỗi',cancelled:'Đã dừng',interrupted:'Gián đoạn'})[job.status]||job.status;badge.className=`badge ${job.status}`;
  $('#cancel-btn').classList.toggle('hidden',!active);$('#exports').classList.toggle('hidden',!job.page_count);
  $('#export-json').href=`/api/jobs/${job.id}/export/json`;$('#export-csv').href=`/api/jobs/${job.id}/export/csv`;
  const pages=job.pages||[];$('#result-count').textContent=job.page_count||pages.length;$('#empty').classList.toggle('hidden',pages.length>0);
  $('#results').innerHTML=pages.map((p,i)=>`<article class="result-card" data-index="${i}"><div class="card-top"><span class="http-ok">● ${p.engine==='browser'?'CHROMIUM':'HTTP'} ${p.status}</span><span>${(p.language||'—').toUpperCase()} · ${fmtDate(p.scraped_at)}</span></div><h3>${esc(p.title||'Không có tiêu đề')}</h3><p>${esc(p.warning||p.description||p.content||(p.word_count===0?'Trang không trả nội dung tĩnh. Hãy thử chế độ Chromium.':'Không có mô tả'))}</p><footer><span>${p.word_count||0} từ</span><span>${p.images?.length||0} ảnh · Xem →</span></footer></article>`).join('');
  document.querySelectorAll('.result-card').forEach(card=>card.onclick=()=>openPage(+card.dataset.index));
}

$('#cancel-btn').onclick=async()=>{if(state.jobId){await fetch(`/api/jobs/${state.jobId}/cancel`,{method:'POST'});poll()}};

function openPage(index){
  const p=state.job.pages[index], modal=$('#detail-modal');
  $('#modal-content').innerHTML=`<div class="modal-body"><h2>${esc(p.title||'Không có tiêu đề')}</h2><p class="modal-meta">${esc(p.url)} · ${p.word_count} từ · HTTP ${p.status}</p><div class="modal-tabs"><button class="active" data-tab="text">Nội dung</button><button data-tab="preview">Giao diện gốc</button><a href="${esc(p.url)}" target="_blank" rel="noopener">Mở website ↗</a></div><div id="tab-text" class="article-content">${esc(p.content)}</div><iframe id="tab-preview" class="preview hidden" sandbox src="/api/jobs/${state.job.id}/pages/${index}/html"></iframe></div>`;
  modal.showModal();modal.querySelectorAll('[data-tab]').forEach(btn=>btn.onclick=()=>{modal.querySelectorAll('[data-tab]').forEach(x=>x.classList.remove('active'));btn.classList.add('active');$('#tab-text').classList.toggle('hidden',btn.dataset.tab!=='text');$('#tab-preview').classList.toggle('hidden',btn.dataset.tab!=='preview')});
}
$('.modal-close').onclick=()=>$('#detail-modal').close();

async function loadHistory(){
  const jobs=await (await fetch('/api/jobs')).json();
  $('#history-list').innerHTML=jobs.length?jobs.map(j=>`<article class="history-item" data-id="${j.id}"><div><h3>${esc(j.url)}</h3><p>${fmtDate(j.created_at)} · ${esc(j.message)}</p></div><div><strong>${j.page_count}</strong><span>TRANG</span></div><div><strong>${esc(j.status)}</strong><span>TRẠNG THÁI</span></div></article>`).join(''):'<div class="empty"><h3>Chưa có lịch sử</h3></div>';
  document.querySelectorAll('.history-item').forEach(el=>el.onclick=async()=>{state.jobId=el.dataset.id;state.job=await (await fetch(`/api/jobs/${state.jobId}`)).json();document.querySelector('[data-view="workspace"]').click();renderJob(state.job);if(['queued','running'].includes(state.job.status))poll()});
}
$('#refresh-history').onclick=loadHistory;
