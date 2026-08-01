const cards = [
    {
        title: "FPS",
        value: "0.0"
    },
    {
        title: "RESOLUTION",
        value: "640 × 480"
    },
    {
        title: "RECORDING",
        value: "OFF"
    },
    {
        title: "DURATION",
        value: "00:00:00"
    },
    {
        title: "FRAMES",
        value: "0"
    }
];

function StatusCards() {

    return (

        <div className="panel cards">

            {
                cards.map((card) => (

                    <div
                        key={card.title}
                        className="metric-card"
                    >

                        <div className="metric-title">

                            {card.title}

                        </div>

                        <div className="metric-value">

                            {card.value}

                        </div>

                    </div>

                ))
            }

        </div>

    );

}

export default StatusCards;