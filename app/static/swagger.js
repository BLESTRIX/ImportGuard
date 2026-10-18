window.addEventListener('DOMContentLoaded', function () {
  window.ui = SwaggerUIBundle({
    url: '/openapi.json', dom_id: '#swagger-ui', deepLinking: true,
    presets: [SwaggerUIBundle.presets.apis], layout: 'BaseLayout',
    validatorUrl: null,
    requestInterceptor: function (request) {
      const token = document.cookie.split('; ').find(c => c.startsWith('ig_csrf='));
      if (token) request.headers['X-CSRF-Token'] = decodeURIComponent(token.split('=')[1]);
      request.credentials = 'same-origin';
      return request;
    }
  });
});
