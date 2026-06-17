# CMakeLists.txt — la recette de build du projet guidance (C++/MAVSDK).
cmake_minimum_required(VERSION 3.15)
project(argos_guidance CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# Trouve MAVSDK installé sur le système (le .deb a posé un MAVSDKConfig.cmake).
find_package(MAVSDK REQUIRED)

# App 1 : portage de mission_basic (connect -> arm -> takeoff -> land).
add_executable(guidance src/main.cpp)
target_link_libraries(guidance MAVSDK::mavsdk)

# App 2 : contrôle yaw-rate continu (loi de guidage Mode B), streamé via Offboard.
add_executable(yaw_track src/yaw_track.cpp)
target_link_libraries(yaw_track MAVSDK::mavsdk)

# App 3 : guidage fixed-wing (ArduPlane) — à comparer avec main.cpp (copter).
add_executable(plane src/plane.cpp)
target_link_libraries(plane MAVSDK::mavsdk)

# Tests unitaires de la loi de commande (doctest, AUCUN drone requis).
enable_testing()
add_executable(test_control_law tests/test_control_law.cpp)
target_include_directories(test_control_law PRIVATE tests src)
add_test(NAME control_law COMMAND test_control_law)
