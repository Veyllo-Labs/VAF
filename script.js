document.getElementById('contactForm').addEventListener('submit', function(e) {
    e.preventDefault();
    const formData = new FormData(this);
    fetch('https://api.formspree.io/make_request', {
        method: 'POST',
        body: formData,
        mode: 'no-cors'
    }).then(function(response) {
        alert('Nachricht gesendet!');
        document.getElementById('contactForm').reset();
    }).catch(function(error) {
        console.error('Fehler:', error);
    });
});