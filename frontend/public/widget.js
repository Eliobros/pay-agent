/*
 * PayAgent widget: adds a floating chat button to any website.
 * Usage: <script src="https://YOUR-PAYAGENT-HOST/widget.js" defer></script>
 * Optional attributes: data-color="#1746d1"  data-open="true"
 */
(function () {
  var script = document.currentScript || document.querySelector('script[src*="widget.js"]');
  if (!script) return;
  var origin = new URL(script.src, location.href).origin;
  var color = script.getAttribute('data-color') || '#1746d1';
  var Z = 2147483000;

  var btn = document.createElement('button');
  btn.type = 'button';
  btn.setAttribute('aria-label', 'Open shopping assistant');
  btn.textContent = '💬';
  btn.style.cssText =
    'position:fixed;right:20px;bottom:20px;width:58px;height:58px;border-radius:50%;border:0;cursor:pointer;' +
    'font-size:26px;color:#fff;background:' + color + ';box-shadow:0 10px 30px rgba(0,0,0,.28);z-index:' + Z;

  var frame = document.createElement('iframe');
  frame.title = 'PayAgent shopping assistant';
  frame.style.cssText =
    'position:fixed;border:0;background:#fff;display:none;z-index:' + (Z + 1) +
    ';box-shadow:0 20px 60px rgba(0,0,0,.35)';

  function layout() {
    var small = window.innerWidth <= 480;
    frame.style.right = small ? '0' : '20px';
    frame.style.bottom = small ? '0' : '90px';
    frame.style.width = small ? '100vw' : '380px';
    frame.style.height = small ? '100%' : 'min(620px, calc(100vh - 110px))';
    frame.style.borderRadius = small ? '0' : '18px';
  }

  var loaded = false;
  function open() {
    if (!loaded) { frame.src = origin + '/embed'; loaded = true; }
    layout();
    frame.style.display = 'block';
    btn.style.display = window.innerWidth <= 480 ? 'none' : 'block';
  }
  function close() {
    frame.style.display = 'none';
    btn.style.display = 'block';
  }

  btn.addEventListener('click', function () {
    frame.style.display === 'block' ? close() : open();
  });
  window.addEventListener('resize', function () {
    if (frame.style.display === 'block') open();
  });
  window.addEventListener('message', function (e) {
    if (e.origin === origin && e.data && e.data.payagent === 'close') close();
  });

  document.body.appendChild(frame);
  document.body.appendChild(btn);
  if (script.getAttribute('data-open') === 'true') open();
})();
