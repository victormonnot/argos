// guidance/src/main.cpp
// ARGOS guidance (C++/MAVSDK) — connect -> arm -> takeoff -> land.
// Portage C++ de sitl/mission_basic.py. SITL only.
//
// Prérequis : un SITL lancé (sitl/run_sitl.sh) qui pousse vers udp:127.0.0.1:14551.

#include <chrono>
#include <iostream>
#include <thread>

#include <mavsdk/mavsdk.h>
#include <mavsdk/plugins/action/action.h>
#include <mavsdk/plugins/telemetry/telemetry.h>

using namespace mavsdk;
using std::chrono::seconds;
using std::this_thread::sleep_for;

int main()
{
    // 1) Ouvrir le lien. Mavsdk = l'objet racine ; on se déclare "station sol".
    Mavsdk mavsdk{Mavsdk::Configuration{ComponentType::GroundStation}};

    // udpin:// => on ÉCOUTE sur 14551 (là où MAVProxy pousse), comme udpin: en Python.
    const ConnectionResult conn = mavsdk.add_any_connection("udpin://0.0.0.0:14551");
    if (conn != ConnectionResult::Success) {
        std::cerr << "Connexion échouée : " << conn << '\n';
        return 1;
    }

    // 2) Attendre la découverte du drone (équivalent de wait_heartbeat()).
    std::cout << "En attente du drone...\n";
    const auto system = mavsdk.first_autopilot(10.0);   // 10 s de timeout
    if (!system) {
        std::cerr << "Aucun drone détecté (le SITL tourne ? bon port ?).\n";
        return 1;
    }
    std::cout << "Drone connecté.\n";

    // 3) Les plugins : Action = on COMMANDE, Telemetry = on LIT.
    Action action{system.value()};
    Telemetry telemetry{system.value()};

    // 4) Attendre que le drone soit prêt (health = EKF/position OK ≈ nos pre-arm checks).
    std::cout << "En attente que le drone soit prêt...\n";
    for (int i = 0; !telemetry.health_all_ok(); ++i) {
        if (i > 30) {                       // garde-fou : pas d'attente infinie
            std::cerr << "Drone jamais prêt (timeout).\n";
            return 1;
        }
        sleep_for(seconds(1));
    }
    std::cout << "Drone prêt.\n";

    // 5) Décoller à 10 m. set_takeoff_altitude PUIS arm PUIS takeoff.
    const float TAKEOFF_ALT = 10.0f;
    action.set_takeoff_altitude(TAKEOFF_ALT);
    telemetry.set_rate_position(2.0);       // 2 Hz de position, pour notre boucle

    std::cout << "Armement...\n";
    if (const Action::Result r = action.arm(); r != Action::Result::Success) {
        std::cerr << "Arm échoué : " << r << '\n';
        return 1;
    }

    std::cout << "Décollage...\n";
    if (const Action::Result r = action.takeoff(); r != Action::Result::Success) {
        std::cerr << "Takeoff échoué : " << r << '\n';
        return 1;
    }

    // 6) Boucle fermée : attendre l'altitude (le motif consigne->feedback, en C++).
    while (telemetry.position().relative_altitude_m < TAKEOFF_ALT * 0.95f) {
        std::cout << "  altitude " << telemetry.position().relative_altitude_m << " m\n";
        sleep_for(seconds(1));
    }
    std::cout << "Altitude atteinte.\n";

    sleep_for(seconds(3));                   // un instant de hover

    // 7) Atterrir, puis attendre le désarmement automatique (LAND désarme au sol).
    std::cout << "Atterrissage...\n";
    if (const Action::Result r = action.land(); r != Action::Result::Success) {
        std::cerr << "Land échoué : " << r << '\n';
        return 1;
    }
    while (telemetry.armed()) {
        sleep_for(seconds(1));
    }
    std::cout << "Posé et désarmé. Mission terminée.\n";
    return 0;
}
