// guidance/src/yaw_track.cpp
// ARGOS — contrôle yaw-rate continu (squelette de la loi de guidage Mode B).
//
// Décolle, puis tourne le nez vers un cap-cible en STREAMANT une consigne de
// vitesse de lacet calculée par la loi de commande (control_law.hpp), puis atterrit.
// Erreur = (cap_cible - cap_courant), simulée. En S5 l'erreur viendra de l'offset
// horizontal de la détection vidéo — la loi et la boucle restent identiques.
//
// SITL only. Prérequis : sitl/run_sitl.sh (push vers udp:127.0.0.1:14551).

#include <chrono>
#include <cmath>
#include <iostream>
#include <thread>

#include <mavsdk/mavsdk.h>
#include <mavsdk/plugins/action/action.h>
#include <mavsdk/plugins/offboard/offboard.h>
#include <mavsdk/plugins/telemetry/telemetry.h>

#include "control_law.hpp"

using namespace mavsdk;
using std::chrono::milliseconds;
using std::chrono::seconds;
using std::this_thread::sleep_for;

// Ramène un angle dans [-180, 180] => "plus court chemin" pour l'erreur de cap.
static double wrap180(double deg)
{
    while (deg > 180.0) deg -= 360.0;
    while (deg < -180.0) deg += 360.0;
    return deg;
}

int main()
{
    Mavsdk mavsdk{Mavsdk::Configuration{ComponentType::GroundStation}};
    if (mavsdk.add_any_connection("udpin://0.0.0.0:14551") != ConnectionResult::Success) {
        std::cerr << "Connexion échouée.\n";
        return 1;
    }
    std::cout << "En attente du drone...\n";
    const auto system = mavsdk.first_autopilot(10.0);
    if (!system) { std::cerr << "Aucun drone détecté.\n"; return 1; }

    Action action{system.value()};
    Telemetry telemetry{system.value()};
    Offboard offboard{system.value()};

    for (int i = 0; !telemetry.health_all_ok(); ++i) {
        if (i > 30) { std::cerr << "Drone jamais prêt.\n"; return 1; }
        sleep_for(seconds(1));
    }

    const float TAKEOFF_ALT = 10.0f;
    action.set_takeoff_altitude(TAKEOFF_ALT);
    telemetry.set_rate_position(4.0);

    std::cout << "Armement + décollage...\n";
    if (action.arm() != Action::Result::Success) { std::cerr << "Arm échoué.\n"; return 1; }
    if (action.takeoff() != Action::Result::Success) { std::cerr << "Takeoff échoué.\n"; return 1; }
    while (telemetry.position().relative_altitude_m < TAKEOFF_ALT * 0.95f)
        sleep_for(milliseconds(500));
    std::cout << "Altitude atteinte.\n";

    // Offboard : il faut envoyer une 1re consigne AVANT de démarrer le mode.
    offboard.set_velocity_body({0.0f, 0.0f, 0.0f, 0.0f});
    if (offboard.start() != Offboard::Result::Success) {
        std::cerr << "Offboard refusé (compat ArduCopter ?). On atterrit proprement.\n";
        action.land();
        return 1;
    }

    // La boucle de guidage : erreur -> loi -> consigne, streamée à 10 Hz.
    const double TARGET_HEADING = 90.0;   // cible : Est (= une détection, plus tard)
    const double KP = 1.5;
    const double MAX_RATE_DPS = 40.0;

    std::cout << "Guidage lacet vers " << TARGET_HEADING << "deg...\n";
    for (int tick = 0; tick < 120; ++tick) {            // ~12 s à 10 Hz
        const double heading = telemetry.attitude_euler().yaw_deg;
        const double error = wrap180(TARGET_HEADING - heading);
        const double yaw_rate = argos::yaw_rate_command(error, KP, MAX_RATE_DPS);

        offboard.set_velocity_body(
            {0.0f, 0.0f, 0.0f, static_cast<float>(yaw_rate)});

        if (tick % 5 == 0)
            std::cout << "  cap " << heading << "deg  err " << error
                      << "deg  -> yaw_rate " << yaw_rate << "deg/s\n";

        if (std::abs(error) < 1.0) { std::cout << "  cap atteint.\n"; break; }
        sleep_for(milliseconds(100));
    }

    offboard.stop();

    std::cout << "Atterrissage...\n";
    action.land();
    while (telemetry.armed()) sleep_for(seconds(1));
    std::cout << "Posé. Démo guidage terminée.\n";
    return 0;
}
