// Tests unitaires de la loi de guidage — AUCUN drone requis.
// On prouve le comportement de la loi avant qu'elle ne touche un aéronef.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include "doctest.h"
#include "control_law.hpp"

using argos::yaw_rate_command;

TEST_CASE("cible centrée => aucune rotation")
{
    CHECK(yaw_rate_command(0.0, 2.0, 40.0) == doctest::Approx(0.0));
}

TEST_CASE("le signe de la consigne suit le signe de l'erreur")
{
    CHECK(yaw_rate_command(10.0, 2.0, 40.0) > 0.0);    // cible à droite => tourne à droite
    CHECK(yaw_rate_command(-10.0, 2.0, 40.0) < 0.0);   // cible à gauche => tourne à gauche
}

TEST_CASE("proportionnel : 2x l'erreur => 2x la consigne (hors saturation)")
{
    const double a = yaw_rate_command(5.0, 2.0, 100.0);
    const double b = yaw_rate_command(10.0, 2.0, 100.0);
    CHECK(b == doctest::Approx(2.0 * a));
}

TEST_CASE("saturation : la consigne ne dépasse jamais max_rate_dps")
{
    CHECK(yaw_rate_command(1000.0, 2.0, 40.0) == doctest::Approx(40.0));
    CHECK(yaw_rate_command(-1000.0, 2.0, 40.0) == doctest::Approx(-40.0));
}
