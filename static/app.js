// Search result cards: hover expands details on desktop (pure CSS).
// Devices without hover (touch/mobile) get tap-to-expand instead.
document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll(".paper-card").forEach(function (card) {
        card.addEventListener("click", function (event) {
            if (event.target.closest("a")) {
                return;
            }
            card.classList.toggle("expanded");
        });
    });
});
