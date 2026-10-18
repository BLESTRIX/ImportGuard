document.addEventListener('htmx:configRequest', function (event) {
  const token = document.cookie.split('; ').find(c => c.startsWith('ig_csrf='));
  if (token) event.detail.headers['X-CSRF-Token'] = decodeURIComponent(token.split('=')[1]);
});
document.addEventListener('htmx:responseError', function (event) {
  const box = document.getElementById('request-error');
  if (!box) return;
  let message = 'The request failed. Reload the page and try again.';
  try { message = JSON.parse(event.detail.xhr.responseText).message || message; }
  catch (_) {
    const page = new DOMParser().parseFromString(event.detail.xhr.responseText, 'text/html');
    message = page.querySelector('.alert-danger')?.textContent || page.querySelector('main p')?.textContent || message;
  }
  box.textContent = message;
  box.hidden = false;
  box.scrollIntoView({behavior: 'smooth', block: 'center'});
});
