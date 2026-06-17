// guidance/src/plane.cpp
// ARGOS — guidage fixed-wing (ArduPlane) via MAVSDK.
//
// CALQUÉ sur main.cpp (copter) pour que les DIFFÉRENCES sautent aux yeux.
// Grosse leçon : sur ArduPlane, l'API haut-niveau de MAVSDK ne suffit pas
// (elle est pensée pour PX4). On retombe sur du MAVLink BRUT, comme pymavlink.
// SITL ArduPlane only. Lance le SITL avec --out udp:127.0.0.1:14551 d'abord.

#include <chrono>
#include <iostream>
#include <thread>

#include <mavsdk/mavsdk.h>
#include <mavsdk/plugins/action/action.h>
#include <mavsdk/plugins/mavlink_passthrough/mavlink_passthrough.h>
#include <mavsdk/plugins/telemetry/telemetry.h>

using namespace mavsdk;
using std::chrono::seconds;
using std::this_thread::sleep_for;

// Numéros de mode de vol ArduPlane (enum ArduPilot)
constexpr float PLANE_TAKEOFF = 13.0f;
constexpr float PLANE_RTL = 11.0f;

int main()
{
    // --- Identique au copter : lien, véhicule, plugins ---
    Mavsdk mavsdk{Mavsdk::Configuration{ComponentType::GroundStation}};
    if (mavsdk.add_any_connection("udpin://0.0.0.0:14551") != ConnectionResult::Success) {
        std::cerr << "Connexion échouée.\n";
        return 1;
    }
    std::cout << "En attente de l'avion...\n";
    const auto system = mavsdk.first_autopilot(10.0);
    if (!system) {
        std::cerr << "Aucun véhicule détecté.\n";
        return 1;
    }

    Action action{system.value()};
    Telemetry telemetry{system.value()};
    MavlinkPassthrough mavlink{system.value()};

    // Helper : passer un mode de vol ArduPlane via COMMAND_LONG brut (MAV_CMD_DO_SET_MODE).
    // C'est ce que MAVSDK fait sous le capot, mais son Action ne nous laisse pas
    // choisir un mode ArduPilot arbitraire -> on le fait nous-mêmes.
    auto set_plane_mode = [&](float mode_number) {
        MavlinkPassthrough::CommandLong c{};
        c.target_sysid = mavlink.get_target_sysid();
        c.target_compid = mavlink.get_target_compid();
        c.command = 176;                 // MAV_CMD_DO_SET_MODE
        c.param1 = 1;                    // MAV_MODE_FLAG_CUSTOM_MODE_ENABLED
        c.param2 = mode_number;          // custom_mode = numéro de mode ArduPlane
        c.param3 = c.param4 = c.param5 = c.param6 = c.param7 = 0.0f;
        return mavlink.send_command_long(c);
    };

    for (int i = 0; !telemetry.health_all_ok(); ++i) {
        if (i > 30) {
            std::cerr << "Pas prêt (timeout).\n";
            return 1;
        }
        sleep_for(seconds(1));
    }

    // ─── DIFF 1 — LE DÉCOLLAGE : un MODE, pas une commande ──────────────────
    // Copter : action.takeoff() (NAV_TAKEOFF) -> saut VERTICAL, puis hover.
    // Plane  : action.takeoff() refusé, ET NAV_TAKEOFF en GUIDED refusé aussi.
    //          La façon ArduPlane = un MODE de vol dédié "TAKEOFF" : on arme,
    //          on bascule en TAKEOFF, l'avion fait son climb-out à la vitesse air.
    std::cout << "Armement...\n";
    if (action.arm() != Action::Result::Success) {
        std::cerr << "Arm échoué.\n";
        return 1;
    }

    std::cout << "Mode TAKEOFF (climb-out automatique, pas vertical)...\n";
    set_plane_mode(PLANE_TAKEOFF);

    // Copter : while(alt < cible) -> on s'arrête à l'altitude et on hover.
    // Plane  : il grimpe ET avance en permanence ; il ne se fige jamais à un point.
    std::cout << "Climb-out... (l'avion vole en continu)\n";
    sleep_for(seconds(30));

    // ─── DIFF 2 — PAS DE LAND VERTICAL ──────────────────────────────────────
    // Copter : action.land() -> descente verticale + désarmement auto.
    // Plane  : pas de land vertical. RTL -> l'avion LOITER au-dessus du home,
    //          il ne se pose PAS (un vrai atterrissage = une séquence d'approche).
    std::cout << "RTL — l'avion va LOITER au-dessus du home (pas d'atterrissage).\n";
    set_plane_mode(PLANE_RTL);

    // ─── DIFF 3 — PAS DE DÉSARMEMENT AUTO ───────────────────────────────────
    // Copter : while(armed) -> LAND désarme tout seul une fois posé.
    // Plane  : il tourne en rond indéfiniment. SITL : `disarm force` pour finir.
    std::cout << "L'avion loite. Regarde QGC, puis `disarm force`.\n";
    return 0;
}
