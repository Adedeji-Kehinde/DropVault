'use strict';

import { initializeApp } from 'https://www.gstatic.com/firebasejs/9.22.2/firebase-app.js';
import { getAuth, createUserWithEmailAndPassword, signInWithEmailAndPassword, signOut } from 'https://www.gstatic.com/firebasejs/9.22.2/firebase-auth.js';

const firebaseConfig = {
    apiKey: "AIzaSyBKPVkWCnxbSo8OJtjX6waT-TAZjUBnNbw",
    authDomain: "dropvault-1.firebaseapp.com",
    projectId: "dropvault-1",
    storageBucket: "dropvault-1.firebasestorage.app",
    messagingSenderId: "765180919592",
    appId: "1:765180919592:web:438a05c1bce19f704f1a1d",
    measurementId: "G-5ZG786M94Q"
};

window.addEventListener("load", function() {
    const app = initializeApp(firebaseConfig);
    const auth = getAuth(app);
    updateUI(document.cookie);
    console.log("hello world load");

    // Sign-up a new user
    document.getElementById("sign-up").addEventListener('click', function() {
        const email = document.getElementById("email").value;
        const password = document.getElementById("password").value;
        createUserWithEmailAndPassword(auth, email, password)
            .then((userCredential) => {
                // Created user
                return userCredential.user.getIdToken();
            })
            .then((token) => {
                document.cookie = "token=" + token + ";path=/;SameSite=Strict";
                window.location = "/";
            })
            .catch((error) => {
                console.log(error.code);
                console.log(error.message);
            });
    });

    // Login an existing user
    document.getElementById("login").addEventListener('click', function() {
        const email = document.getElementById("email").value;
        const password = document.getElementById("password").value;
        signInWithEmailAndPassword(auth, email, password)
            .then((userCredential) => {
                console.log("login successful");
                return userCredential.user.getIdToken();
            })
            .then((token) => {
                document.cookie = "token=" + token + ";path=/;SameSite=Strict";
                window.location = "/";
            })
            .catch((error) => {
                console.log(error.code);
                console.log(error.message);
            });
    });

    // Logout an existing user
    document.getElementById("sign-out").addEventListener('click', function() {
        signOut(auth)
            .then(() => {
                document.cookie = "token=;path=/;SameSite=Strict";
                window.location = "/";
            })
            .catch((error) => {
                console.log(error);
            });
    });
});

function updateUI(cookie) {
    const token = parseCookieToken(cookie);
    if (token.length > 0) {
        document.getElementById("login-box").hidden = true;
        document.getElementById("sign-out").hidden = false;
    } else {
        document.getElementById("login-box").hidden = false;
        document.getElementById("sign-out").hidden = true;
    }
}

function parseCookieToken(cookie) {
    const parts = cookie.split(";");
    for (let part of parts) {
        const [key, value] = part.split("=");
        if (key && key.trim() === "token") {
            return value.trim();
        }
    }
    return "";
}
