(function () {
    // Simple autoplay slider for benefits
    const track = document.getElementById('benefitsTrack');
    if (!track) return;

    const cards = Array.from(track.children);
    const controlsContainer = document.getElementById('benefitsControls');
    const controlsMobile = document.getElementById('benefitsControlsMobile');
    let index = 0;
    const total = cards.length;

    function update() {
        const cardWidth = cards[0].getBoundingClientRect().width;
        const spacing = parseInt(getComputedStyle(track).gap, 10) || 20;
        const move = (cardWidth + spacing) * index;
        track.scrollTo({ left: move, behavior: 'smooth' });

        const dots = (controlsContainer || controlsMobile).querySelectorAll('.benefit-dot');
        dots.forEach((d, i) => d.classList.toggle('active', i === index));
    }

    function goNext() {
        index = (index + 1) % total;
        update();
    }

    function buildControls(container) {
        if (!container) return;
        container.innerHTML = '';
        for (let i = 0; i < total; i++) {
            const dot = document.createElement('div');
            dot.className = 'benefit-dot' + (i === 0 ? ' active' : '');
            dot.addEventListener('click', () => {
                index = i;
                update();
            });
            container.appendChild(dot);
        }
    }

    buildControls(controlsContainer);
    buildControls(controlsMobile);
    update();

    let timer = setInterval(goNext, 4500);

    track.addEventListener('mouseenter', () => clearInterval(timer));
    track.addEventListener('mouseleave', () => timer = setInterval(goNext, 4500));

    window.addEventListener('resize', update);
})();