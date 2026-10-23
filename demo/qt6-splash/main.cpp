#include <QApplication>
#include <QCoreApplication>
#include <QCheckBox>
#include <QDir>
#include <QFileInfo>
#include <QFrame>
#include <QHBoxLayout>
#include <QLabel>
#include <QPainter>
#include <QPixmap>
#include <QPushButton>
#include <QString>
#include <QStringList>
#include <QSvgRenderer>
#include <QTimer>
#include <QVBoxLayout>
#include <QVector>
#include <QWidget>

#include <iostream>

static QPixmap imageToPixmap(const QString &path, int size)
{
    if (path.endsWith(QStringLiteral(".svg"), Qt::CaseInsensitive)) {
        QSvgRenderer renderer(path);
        if (!renderer.isValid()) {
            return {};
        }
        QPixmap pixmap(size, size);
        pixmap.fill(Qt::transparent);
        QPainter painter(&pixmap);
        renderer.render(&painter, QRectF(0, 0, size, size));
        return pixmap;
    }
    QPixmap pixmap(path);
    return pixmap.isNull() ? QPixmap() :
        pixmap.scaled(size, size, Qt::IgnoreAspectRatio, Qt::SmoothTransformation);
}

static QLabel *bullet(const QString &text)
{
    auto *label = new QLabel(QStringLiteral("•  ") + text);
    label->setWordWrap(true);
    label->setStyleSheet(QStringLiteral(
        "color: #d9e2ef; font-size: 14px; line-height: 1.45;"));
    return label;
}

int main(int argc, char **argv)
{
    QApplication app(argc, argv);

    QString assetDir = QCoreApplication::applicationDirPath()
        + QStringLiteral("/assets");
    if (!QFileInfo::exists(assetDir)) {
        assetDir = QDir::currentPath() + QStringLiteral("/assets");
    }
    QString imagePath = assetDir + QStringLiteral("/infographic-how-it-works.svg");
    QString screenshot;
    for (int i = 1; i < argc; ++i) {
        const QString arg = QString::fromLocal8Bit(argv[i]);
        if (arg.startsWith(QStringLiteral("--screenshot="))) {
            screenshot = arg.mid(QStringLiteral("--screenshot=").size());
        } else if (!arg.startsWith(QStringLiteral("--"))) {
            imagePath = arg;
        }
    }

    QPixmap source = imageToPixmap(imagePath, 768);
    if (source.isNull()) {
        std::cerr << "No se pudo cargar la imagen: "
                  << imagePath.toStdString() << "\n";
        return 1;
    }

    const QStringList slidePaths{
        imagePath,
        assetDir + QStringLiteral("/infographic-account-risk.svg"),
        assetDir + QStringLiteral("/infographic-protected-content.svg"),
    };
    QVector<QPixmap> slides;
    for (const QString &path : slidePaths) {
        if (!QFileInfo::exists(path)) {
            continue;
        }
        const QPixmap pixmap = imageToPixmap(path, 768);
        if (!pixmap.isNull()) {
            slides.append(pixmap);
        }
    }
    if (slides.isEmpty()) {
        slides.append(source);
    }

    QWidget window;
    window.setWindowTitle(QStringLiteral("Darling-lol · Splash demo (not integrated)"));
    window.setStyleSheet(QStringLiteral(
        "QWidget { background: #11161f; color: #e8eef7; }"
        "QFrame#card { background: #1a2230; border: 1px solid #2b3748;"
        " border-radius: 22px; }"
        "QFrame#imageCard { background: #f4efe3; border: 1px solid #334155;"
        " border-radius: 22px; }"
        "QLabel#eyebrow { color: #e7c66b; font-size: 11px;"
        " font-weight: 700; letter-spacing: 2px; }"
        "QLabel#title { color: #ffffff; font-size: 30px; font-weight: 700; }"
        "QLabel#subtitle { color: #9dacbf; font-size: 13px; }"
        "QLabel#note { color: #9dacbf; font-size: 12px; }"
        "QLabel { background: transparent; }"
        "QCheckBox { background: transparent; color: #d9e2ef;"
        " font-size: 13px; spacing: 10px; }"
        "QCheckBox::indicator { width: 17px; height: 17px;"
        " border: 1px solid #7f8da3; border-radius: 4px;"
        " background: #11161f; }"
        "QCheckBox::indicator:checked { background: #e7c66b;"
        " border-color: #e7c66b; }"
        "QPushButton { background: #e7c66b; color: #141a22; border: none;"
        " border-radius: 10px; padding: 12px 20px; font-size: 14px;"
        " font-weight: 700; }"
        "QPushButton:disabled { background: #3a4350; color: #8793a6; }"
        "QPushButton#navButton { background: #182335; color: #d9e2ef;"
        " border: 1px solid #32445e; border-radius: 9px;"
        " padding: 10px 16px; font-size: 12px; font-weight: 600; }"
        "QPushButton#navButton:hover { background: #22324a; }"));
    window.resize(1400, 900);

    auto *outer = new QHBoxLayout(&window);
    outer->setContentsMargins(32, 32, 32, 32);
    outer->setSpacing(28);

    auto *imageCard = new QFrame;
    imageCard->setObjectName(QStringLiteral("imageCard"));
    imageCard->setFixedSize(768, 768);
    auto *imageLayout = new QVBoxLayout(imageCard);
    imageLayout->setContentsMargins(0, 0, 0, 0);
    auto *image = new QLabel;
    image->setPixmap(slides.first());
    image->setScaledContents(true);
    image->setFixedSize(768, 768);
    imageLayout->addWidget(image);

    auto *leftColumn = new QVBoxLayout;
    leftColumn->setSpacing(12);
    leftColumn->addWidget(imageCard);

    auto *navigation = new QHBoxLayout;
    auto *previousButton = new QPushButton(QStringLiteral("‹ Previous"));
    auto *nextButton = new QPushButton(QStringLiteral("Next ›"));
    previousButton->setObjectName(QStringLiteral("navButton"));
    nextButton->setObjectName(QStringLiteral("navButton"));
    navigation->addWidget(previousButton);
    navigation->addStretch();
    navigation->addWidget(nextButton);
    leftColumn->addLayout(navigation);
    outer->addLayout(leftColumn, 0);

    auto *card = new QFrame;
    card->setObjectName(QStringLiteral("card"));
    auto *content = new QVBoxLayout(card);
    content->setContentsMargins(30, 28, 30, 28);
    content->setSpacing(12);

    auto *eyebrow = new QLabel(
        QStringLiteral("SPLASH DEMO · QT6 · 1 / %1").arg(slides.size()));
    eyebrow->setObjectName(QStringLiteral("eyebrow"));
    content->addWidget(eyebrow);

    auto *title = new QLabel(QStringLiteral("Darling-lol"));
    title->setObjectName(QStringLiteral("title"));
    content->addWidget(title);

    auto *subtitle = new QLabel(
        QStringLiteral("Compatibility notice · unofficial project"));
    subtitle->setObjectName(QStringLiteral("subtitle"));
    content->addWidget(subtitle);
    content->addSpacing(8);

    content->addWidget(bullet(QStringLiteral(
        "We are not affiliated with, endorsed by, or approved by Riot Games.")));
    content->addWidget(bullet(QStringLiteral(
        "We are an unofficial compatibility layer for running the macOS "
        "client on Linux.")));
    content->addWidget(bullet(QStringLiteral(
        "We do not distribute Riot binaries, game assets, or protected "
        "content.")));
    content->addWidget(bullet(QStringLiteral(
        "We do not include DRM, anti-cheat, or telemetry bypasses.")));
    content->addWidget(bullet(QStringLiteral(
        "Riot does not support this environment. Use may violate the Terms "
        "of Service and lead to account sanctions, including suspensions.")));

    content->addStretch();

    auto *confirm = new QCheckBox(
        QStringLiteral("I understand the scope and account risk."));
    content->addWidget(confirm);

    auto *continueButton = new QPushButton(QStringLiteral("Continue"));
    continueButton->setEnabled(false);
    QObject::connect(confirm, &QCheckBox::toggled, continueButton,
                     &QPushButton::setEnabled);
    QObject::connect(continueButton, &QPushButton::clicked, &window,
                     &QWidget::close);
    content->addWidget(continueButton);

    auto *note = new QLabel(QStringLiteral(
        "Demo only: not integrated; installs and modifies nothing."));
    note->setObjectName(QStringLiteral("note"));
    content->addWidget(note);

    outer->addWidget(card, 1);

    int currentSlide = 0;
    const int slideCount = static_cast<int>(slides.size());
    auto showSlide = [&](int delta) {
        currentSlide = (currentSlide + delta + slideCount) % slideCount;
        image->setPixmap(slides[currentSlide]);
        eyebrow->setText(QStringLiteral("SPLASH DEMO · QT6 · %1 / %2")
                             .arg(currentSlide + 1)
                             .arg(slideCount));
    };

    auto *timer = new QTimer(&window);
    timer->setInterval(3000);
    QObject::connect(timer, &QTimer::timeout, &window, [&] { showSlide(1); });
    QObject::connect(previousButton, &QPushButton::clicked, &window, [&] {
        showSlide(-1);
        timer->start();
    });
    QObject::connect(nextButton, &QPushButton::clicked, &window, [&] {
        showSlide(1);
        timer->start();
    });

    window.show();

    if (!screenshot.isEmpty()) {
        QApplication::processEvents();
        const QPixmap capture = window.grab();
        if (!capture.save(screenshot)) {
            std::cerr << "No se pudo guardar la captura: "
                      << screenshot.toStdString() << "\n";
            return 1;
        }
        std::cout << "captura=" << screenshot.toStdString() << "\n";
        return 0;
    }

    timer->start();
    return app.exec();
}
