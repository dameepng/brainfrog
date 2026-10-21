(function () {
  var form = document.getElementById('login-form');
  var email = document.getElementById('email');
  var error = document.getElementById('email-error');
  var EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
  form.addEventListener('submit', function (event) {
    event.preventDefault();
    if (!EMAIL_RE.test(email.value)) {
      error.hidden = false;
      return;
    }
    error.hidden = true;
    window.location.assign('/dashboard');
  });
})();
